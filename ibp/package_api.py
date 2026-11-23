"""Package endpoints: the books recorded in a filled request, and their
registration on the TDCJ Book Portal.

Rules (see docs/book-registration.md in the meta repo):

- Only ``Filled`` requests have items; a ``Tossed`` request has no package.
- Every add, edit or delete of an item increments the request's
  ``contents_revision`` in the same transaction; every edit increments the
  item's ``revision``. Edits and registrations carry the revision they were
  based on and are refused (409) when it is stale.
- A registered item is read-only (the portal entry can't change either). It
  can be deleted with ``confirm_registered=true``; its ``registrations`` row
  stays.
- Only Texas items are registered; the queue is every Texas item on a filled
  request not registered yet.

Concurrency: each write starts with a single ``UPDATE`` (the request's
``contents_revision = contents_revision + 1``, or the conditional
registration update), so on SQLite the transaction takes the write lock
straight away and a concurrent write to the same database waits instead of
failing or losing an increment.
"""

import datetime
import logging
from typing import Annotated

import sqlalchemy
from fastapi import Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import models, schemas, warnings
from .api import get_session
from .base import app
from .labels import RequestKey, label_package_id
from .request_api import load_request, request_key
from .titles import title_key

logger = logging.getLogger(__name__)

REQUEST_NOT_FOUND = "Request not found."
ITEM_NOT_FOUND = "Item not found."

# An item id path parameter; out of range is a 422, before it reaches SQLite.
ItemId = Annotated[int, Path(ge=1, le=schemas.MAX_DB_INT)]


def now() -> datetime.datetime:
    """The current time in UTC."""
    return datetime.datetime.now(datetime.timezone.utc)


def conflict(detail: str) -> HTTPException:
    """A 409 with a sentence detail."""
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def item_belongs_to(key: RequestKey):
    """Filter package items to the request with ``key``."""
    return sqlalchemy.and_(
        models.PackageItem.inmate_jurisdiction == key.jurisdiction,
        models.PackageItem.inmate_id == key.inmate_id,
        models.PackageItem.request_index == key.index,
    )


def request_code(jurisdiction: str, inmate_id: int, index: int) -> str:
    """The request code for a request key (via the one formatter)."""
    return label_package_id(
        models.Request(
            inmate_jurisdiction=jurisdiction, inmate_id=inmate_id, index=index
        )
    )


def tdcj_number(inmate_id: int) -> str:
    """An inmate number as the portal shows it (zero-padded to 8 digits)."""
    return f"{inmate_id:08d}"


async def update_contents_revision(
    session: AsyncSession, key: RequestKey, increment: int
) -> None:
    """Add ``increment`` to a request's ``contents_revision``, or 404.

    With ``increment=0`` it changes nothing but still takes SQLite's write
    lock, for a write that only knows later whether the contents change.
    """
    found = (
        await session.execute(
            sqlalchemy.update(models.Request)
            .where(
                models.Request.inmate_jurisdiction == key.jurisdiction,
                models.Request.inmate_id == key.inmate_id,
                models.Request.index == key.index,
            )
            .values(contents_revision=models.Request.contents_revision + increment)
            .returning(models.Request.index)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=REQUEST_NOT_FOUND)


async def bump_contents_revision(session: AsyncSession, code: str) -> RequestKey:
    """Increment a request's ``contents_revision``, or 404 if it doesn't exist.

    Meant as the first statement of a write transaction (see the module
    docstring); raising later in the transaction rolls the increment back.
    """
    key = request_key(code)
    await update_contents_revision(session, key, 1)
    return key


async def load_item(
    session: AsyncSession, key: RequestKey, item_id: int
) -> models.PackageItem:
    """Return an item of the request with ``key``, or 404."""
    item = (
        await session.execute(
            select(models.PackageItem).where(
                models.PackageItem.id == item_id, item_belongs_to(key)
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ITEM_NOT_FOUND)
    return item


def check_not_tossed(request: models.Request) -> None:
    """A tossed request has no package (409)."""
    if request.action != schemas.ActionEnum.FILLED.value:
        raise conflict("Request was tossed, so it has no package.")


async def previously_sent(
    session: AsyncSession, key: RequestKey, isbn: str | None
) -> list[str]:
    """Codes of the inmate's other requests already containing ``isbn``."""
    if isbn is None:
        return []
    indexes = (
        await session.execute(
            select(models.PackageItem.request_index)
            .where(
                models.PackageItem.inmate_jurisdiction == key.jurisdiction,
                models.PackageItem.inmate_id == key.inmate_id,
                models.PackageItem.request_index != key.index,
                models.PackageItem.isbn == isbn,
            )
            .distinct()
            .order_by(models.PackageItem.request_index)
        )
    ).scalars()
    return [request_code(key.jurisdiction, key.inmate_id, index) for index in indexes]


async def save_to_book_cache(session: AsyncSession, item: models.PackageItem) -> None:
    """Upsert an item's title and author into the ISBN lookup cache.

    The providers' raw ``candidates`` are kept on an existing row so the
    original answers can still be audited.
    """
    assert item.isbn is not None
    book = await session.get(models.Book, item.isbn)
    if book is None:
        book = models.Book(isbn=item.isbn)
        session.add(book)
    book.title = item.title
    book.author = item.author
    book.source = "manual"
    book.confidence = None
    book.fetched_at = now()


@app.get("/requests/{code}", response_model=schemas.Package)
async def get_package(code: str, session: AsyncSession = Depends(get_session)):
    """Get a filled request with its inmate, books and the inmate's warnings.

    Read-only: does not record a lookup or query the inmate providers. Only
    inmate warnings are given (the request postmark warning would always
    flag the request itself). A tossed request has no package (409).
    """
    request = await load_request(
        session,
        code,
        selectinload(models.Request.inmate).selectinload(models.Inmate.unit),
        selectinload(models.Request.items),
    )
    check_not_tossed(request)
    return schemas.Package(
        request=schemas.Request.model_validate(request),
        inmate=schemas.PackageInmate.model_validate(request.inmate),
        items=[schemas.PackageItem.model_validate(item) for item in request.items],
        warnings=schemas.InmateWarnings(**warnings.inmate(request.inmate)),
    )


@app.get("/requests/{code}/items", response_model=list[schemas.PackageItem])
async def get_package_items(code: str, session: AsyncSession = Depends(get_session)):
    """List the books recorded for a request, oldest first.

    shippy uses this to refuse a Texas package with any book not registered.
    A tossed request has no package (409).
    """
    request = await load_request(session, code, selectinload(models.Request.items))
    check_not_tossed(request)
    return request.items


@app.post(
    "/requests/{code}/items",
    response_model=schemas.PackageItemCreated,
    status_code=status.HTTP_201_CREATED,
)
async def add_package_item(
    code: str,
    item_data: schemas.PackageItemCreate,
    session: AsyncSession = Depends(get_session),
):
    """Record a book in a filled request's package.

    The new item is not registered yet. The response's ``previously_sent``
    lists this inmate's other requests that already have the same ISBN.
    """
    async with session.begin():
        key = await bump_contents_revision(session, code)
        request = await load_request(session, code)
        if request.action != schemas.ActionEnum.FILLED.value:
            raise conflict("Only filled requests can have books recorded.")

        timestamp = now()
        item = models.PackageItem(
            inmate_jurisdiction=key.jurisdiction,
            inmate_id=key.inmate_id,
            request_index=key.index,
            revision=0,
            isbn=item_data.isbn,
            title=item_data.title,
            author=item_data.author,
            source=item_data.source.value,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(item)
        await session.flush()

        logger.debug("added package item %d", item.id)
        return schemas.PackageItemCreated(
            **schemas.PackageItem.model_validate(item).model_dump(),
            previously_sent=await previously_sent(session, key, item.isbn),
        )


@app.put("/requests/{code}/items/{item_id}", response_model=schemas.PackageItem)
async def update_package_item(
    code: str,
    item_id: ItemId,
    item_data: schemas.PackageItemUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Edit a package item's title, author and ISBN.

    409 if the item was edited since ``revision`` or is registered (read-only).
    The ISBN lookup cache changes only with ``update_cache`` and an ISBN.
    A changed title or author makes the item's source ``manual``.
    """
    async with session.begin():
        key = await bump_contents_revision(session, code)
        item = await load_item(session, key, item_id)
        if item.registered_at is not None:
            raise conflict("The book is registered on the portal, so it can't change.")
        if item.revision != item_data.revision:
            raise conflict("The book was changed by someone else; reload it.")

        if (item.title, item.author) != (item_data.title, item_data.author):
            item.source = schemas.ItemSourceEnum.MANUAL.value
        item.isbn = item_data.isbn
        item.title = item_data.title
        item.author = item_data.author
        item.revision += 1
        item.updated_at = now()

        if item_data.update_cache and item.isbn is not None:
            await save_to_book_cache(session, item)

        await session.flush()
        logger.debug("updated package item %d", item.id)
        return schemas.PackageItem.model_validate(item)


@app.delete("/requests/{code}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_package_item(
    code: str,
    item_id: ItemId,
    confirm_registered: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """Delete a package item.

    A registered item needs ``confirm_registered=true`` (409 otherwise): it
    stays registered on the portal, and its ``registrations`` row stays.
    """
    async with session.begin():
        key = await bump_contents_revision(session, code)
        item = await load_item(session, key, item_id)
        if item.registered_at is not None and not confirm_registered:
            raise conflict(
                "The book is registered on the portal; confirm to delete it."
            )
        logger.debug("deleting package item %d", item.id)
        await session.delete(item)


@app.post(
    "/requests/{code}/items/{item_id}/registrations",
    response_model=schemas.PackageItem,
    status_code=status.HTTP_201_CREATED,
)
async def register_package_item(
    code: str,
    item_id: ItemId,
    registration: schemas.RegistrationCreate,
    session: AsyncSession = Depends(get_session),
):
    """Record that a book was registered on the TDCJ Book Portal.

    ``revision`` is the item revision the portal form was filled from. Sets
    the item's ``registered_at`` and appends a ``registrations`` row. 409 if
    the item changed since, is already registered, or is not a Texas item.
    The package contents don't change, so ``contents_revision`` doesn't.
    """
    key = request_key(code)
    async with session.begin():
        timestamp = now()
        registered = (
            await session.execute(
                sqlalchemy.update(models.PackageItem)
                .where(
                    models.PackageItem.id == item_id,
                    item_belongs_to(key),
                    models.PackageItem.inmate_jurisdiction
                    == schemas.JurisdictionEnum.TEXAS.value,
                    models.PackageItem.revision == registration.revision,
                    models.PackageItem.registered_at.is_(None),
                )
                .values(registered_at=timestamp)
                .returning(models.PackageItem.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()

        if registered is None:
            await load_request(session, code)
            item = await load_item(session, key, item_id)
            if item.inmate_jurisdiction != schemas.JurisdictionEnum.TEXAS.value:
                raise conflict("Only Texas books are registered on the portal.")
            if item.registered_at is not None:
                raise conflict("The book is already registered.")
            raise conflict("The book was changed since the form was filled.")

        item = await load_item(session, key, item_id)
        session.add(
            models.Registration(
                item_id=item.id,
                inmate_jurisdiction=key.jurisdiction,
                inmate_id=key.inmate_id,
                request_index=key.index,
                title=item.title,
                author=item.author,
                tdcj_number=tdcj_number(key.inmate_id),
                registered_at=timestamp,
            )
        )
        await session.flush()
        logger.debug("registered package item %d", item.id)
        return schemas.PackageItem.model_validate(item)


async def matching_item(
    session: AsyncSession, key: RequestKey, report: schemas.PortalRegistrationCreate
) -> models.PackageItem | None:
    """The unregistered item of a package that a portal report is about.

    First the oldest with the reported ISBN, else the oldest with the same
    title (compared by ``titles.title_key``: case, accents, punctuation and
    the article ignored) and no other ISBN, else none.
    """
    items = (
        await session.execute(
            select(models.PackageItem)
            .where(item_belongs_to(key), models.PackageItem.registered_at.is_(None))
            .order_by(models.PackageItem.id)
        )
    ).scalars()
    candidates = list(items)
    if report.isbn is not None:
        for item in candidates:
            if item.isbn == report.isbn:
                return item
    title = title_key(report.title)
    for item in candidates:
        if item.isbn in (None, report.isbn) and title_key(item.title) == title:
            return item
    return None


@app.post(
    "/requests/{code}/portal-registrations",
    response_model=schemas.PackageItem,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "A retry of a report already recorded."}},
)
async def record_portal_registration(
    code: str,
    report: schemas.PortalRegistrationCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Record a book the portal just saved for this package (extension scan mode).

    In one transaction: the package's matching unregistered book (see
    ``matching_item``) is marked registered, or, if there is none, a new
    book is recorded already registered; then a ``registrations`` row is
    appended. A matched book takes the submitted title and author (what TDCJ
    has now), and its ISBN if it had none. The same confirmation number
    reported again for this request with the same title returns the book it
    recorded (200).

    409 for a tossed or federal request, a ``tdcj_number`` that isn't the
    request's inmate, or a confirmation number already recorded for this
    request with another title.
    """
    key = request_key(code)
    async with session.begin():
        await update_contents_revision(session, key, 0)
        request = await load_request(session, code)
        check_not_tossed(request)
        if key.jurisdiction != schemas.JurisdictionEnum.TEXAS.value:
            raise conflict("Only Texas books are registered on the portal.")
        if report.tdcj_number != tdcj_number(key.inmate_id):
            raise conflict("The portal shipment is for a different inmate.")

        recorded = (
            await session.execute(
                select(models.Registration)
                .where(
                    models.Registration.inmate_jurisdiction == key.jurisdiction,
                    models.Registration.inmate_id == key.inmate_id,
                    models.Registration.request_index == key.index,
                    models.Registration.confirmation_number
                    == report.confirmation_number,
                )
                .order_by(models.Registration.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if recorded is not None:
            # Only the same book again is a retry; another book under a
            # confirmation number already used must not be dropped silently.
            if recorded.title.casefold() != report.title.casefold():
                raise conflict(
                    "That confirmation number is already recorded for another book."
                )
            item = await session.get(models.PackageItem, recorded.item_id)
            if item is None:
                raise conflict(
                    "The registration was recorded, but its book was deleted."
                )
            response.status_code = status.HTTP_200_OK
            return schemas.PackageItem.model_validate(item)

        timestamp = now()
        item = await matching_item(session, key, report)
        if item is None:
            item = models.PackageItem(
                inmate_jurisdiction=key.jurisdiction,
                inmate_id=key.inmate_id,
                request_index=key.index,
                revision=0,
                isbn=report.isbn,
                title=report.title,
                author=report.author,
                source=(
                    schemas.ItemSourceEnum.ISBN
                    if report.isbn is not None
                    else schemas.ItemSourceEnum.MANUAL
                ).value,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(item)
            await update_contents_revision(session, key, 1)
        else:
            isbn = item.isbn if item.isbn is not None else report.isbn
            if (item.isbn, item.title, item.author) != (
                isbn,
                report.title,
                report.author,
            ):
                if (item.title, item.author) != (report.title, report.author):
                    item.source = schemas.ItemSourceEnum.MANUAL.value
                item.isbn = isbn
                item.title = report.title
                item.author = report.author
                item.revision += 1
                item.updated_at = timestamp
                await update_contents_revision(session, key, 1)

        item.registered_at = timestamp
        item.confirmation_number = report.confirmation_number
        await session.flush()
        session.add(
            models.Registration(
                item_id=item.id,
                inmate_jurisdiction=key.jurisdiction,
                inmate_id=key.inmate_id,
                request_index=key.index,
                title=item.title,
                author=item.author,
                tdcj_number=report.tdcj_number,
                registered_at=timestamp,
                confirmation_number=report.confirmation_number,
            )
        )
        await session.flush()
        logger.debug("recorded portal registration of package item %d", item.id)
        return schemas.PackageItem.model_validate(item)


@app.get("/registration-queue", response_model=list[schemas.RegistrationQueueEntry])
async def get_registration_queue(session: AsyncSession = Depends(get_session)):
    """Every Texas book on a filled request not registered on the portal yet.

    Grouped by request, oldest request (by date processed) first, and in the
    order recorded within a request.
    """
    rows = await session.execute(
        select(models.PackageItem)
        .join(models.PackageItem.request)
        .where(
            models.PackageItem.inmate_jurisdiction
            == schemas.JurisdictionEnum.TEXAS.value,
            models.PackageItem.registered_at.is_(None),
            models.Request.action == schemas.ActionEnum.FILLED.value,
        )
        .order_by(
            models.Request.date_processed,
            models.Request.inmate_id,
            models.Request.index,
            models.PackageItem.id,
        )
    )
    return [
        schemas.RegistrationQueueEntry(
            code=request_code(
                item.inmate_jurisdiction, item.inmate_id, item.request_index
            ),
            item_id=item.id,
            revision=item.revision,
            title=item.title,
            author=item.author,
            tdcj_number=tdcj_number(item.inmate_id),
        )
        for item in rows.scalars()
    ]
