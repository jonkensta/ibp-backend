"""ISBN and UPC lookup endpoints (see ``ibp.books`` and ``ibp.upcs``)."""

import datetime
from dataclasses import asdict, dataclass
from typing import Optional, cast

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import books, models, schemas, upcs
from .api import get_session
from .base import app

# How long a UPC that no service maps to an ISBN is answered from the cache
# before the service is asked again.
UPC_MISS_TTL = datetime.timedelta(days=30)


def now() -> datetime.datetime:
    """The current time in UTC."""
    return datetime.datetime.now(datetime.timezone.utc)


async def book_for_isbn(session: AsyncSession, isbn: str) -> schemas.Book:
    """The book for a normalized ISBN-13: from the cache, else looked up.

    A cacheable lookup result is stored in the ``books`` cache; no result is
    a 404.
    """
    cached = await session.get(models.Book, isbn)
    if cached is not None:
        return schemas.Book.model_validate(cached)

    result = await books.lookup(isbn)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Book not found."
        )

    book = models.Book(
        isbn=isbn,
        title=result.title,
        author=result.author,
        source=result.source,
        confidence=result.confidence,
        candidates=result.candidates,
        fetched_at=now(),
    )
    response = schemas.Book.model_validate(book)
    if result.cacheable:
        session.add(book)
        try:
            await session.commit()
        except IntegrityError:
            # Another request cached the same ISBN first; that's fine.
            await session.rollback()
    return response


@app.get("/books/{isbn}", response_model=schemas.Book)
async def get_book(isbn: str, session: AsyncSession = Depends(get_session)):
    """Title and author for an ISBN-10/13 (or 18-digit EAN with a price add-on).

    Answers from the ``books`` cache, else asks the providers and has Claude
    merge their answers (see ``ibp.books``), caching the result. A provider
    fallback taken because Claude failed is returned but not cached.
    """
    normalized = books.normalize_isbn(isbn)
    if normalized is None:
        raise HTTPException(
            status_code=422,
            detail="Not a valid ISBN.",
        )
    return await book_for_isbn(session, normalized)


def check_upc_code(code: str) -> None:
    """422 unless ``code`` is a UPC-A with an optional 2- or 5-digit add-on."""
    if not upcs.valid_code(code):
        raise HTTPException(
            status_code=422,
            detail="Not a valid UPC (12 digits, plus an optional 2- or 5-digit "
            "add-on).",
        )


def no_isbn() -> JSONResponse:
    """The 404 for a UPC with no known ISBN."""
    body = schemas.UpcNotFound(detail="No ISBN known for this UPC.", reason="no_isbn")
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=body.model_dump(exclude_none=True),
    )


def no_book(isbn: str) -> JSONResponse:
    """The 404 for a UPC whose ISBN no provider knows."""
    body = schemas.UpcNotFound(detail="Book not found.", reason="no_book", isbn=isbn)
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content=body.model_dump()
    )


@dataclass
class UpcRow:
    """A snapshot of one ``upcs`` row."""

    isbn: Optional[str]
    source: str
    looked_up_at: datetime.datetime


async def read_upc(session: AsyncSession, code: str) -> Optional[UpcRow]:
    """The committed ``upcs`` row for ``code`` (bypassing the identity map)."""
    result = await session.execute(
        sa.select(models.Upc.isbn, models.Upc.source, models.Upc.looked_up_at).where(
            models.Upc.upc == code
        )
    )
    row = result.first()
    return None if row is None else UpcRow(*row)


async def write_upc(
    session: AsyncSession, code: str, isbn: Optional[str], source: str
) -> Optional[UpcRow]:
    """Store ``isbn`` for ``code`` unless the row already has an ISBN.

    Atomic: a miss (NULL ISBN) is replaced with a conditional UPDATE, else
    the row is inserted; a row that has an ISBN, or that another request
    inserted first, is kept. Returns the row as it now stands, so the caller
    answers from the winner.
    """
    ours = UpcRow(isbn=isbn, source=source, looked_up_at=now())
    values = asdict(ours)
    for _attempt in range(2):
        result = await session.execute(
            sa.update(models.Upc)
            .where(models.Upc.upc == code, models.Upc.isbn.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, result).rowcount:
            await session.commit()
            return ours
        try:
            await session.execute(sa.insert(models.Upc).values(upc=code, **values))
            await session.commit()
            return ours
        except IntegrityError:
            await session.rollback()
        row = await read_upc(session, code)
        if row is None or row.isbn is not None:
            return row
        # A miss appeared between the UPDATE and the INSERT: try once more.
    return await read_upc(session, code)


async def upc_isbn(session: AsyncSession, code: str) -> UpcRow | JSONResponse:
    """The ``upcs`` row giving ``code``'s ISBN, else the no_isbn 404.

    A volunteer's mapping of the full code (with its add-on) wins. Else the
    12-digit core is answered from the cache, or asked of UPCitemdb (which
    knows no add-ons), and that answer, hit or miss, is cached under the
    core; a miss is retried after ``UPC_MISS_TTL``.
    """
    core = code[:12]
    if upcs.has_add_on(code):
        full = await read_upc(session, code)
        if full is not None and full.isbn is not None:
            return full

    row = await read_upc(session, core)
    if row is None or (row.isbn is None and now() - row.looked_up_at >= UPC_MISS_TTL):
        try:
            found = await upcs.upcitemdb(core)
        except upcs.UpcServiceUnavailable as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error
        row = await write_upc(session, core, found, "upcitemdb")
        if upcs.has_add_on(code):
            # A volunteer may have taught the full code meanwhile.
            full = await read_upc(session, code)
            if full is not None and full.isbn is not None:
                return full

    if row is None or row.isbn is None:
        return no_isbn()
    return row


@app.get(
    "/books/upc/{code}",
    response_model=schemas.UpcBook,
    responses={404: {"model": schemas.UpcNotFound}},
)
async def get_book_by_upc(code: str, session: AsyncSession = Depends(get_session)):
    """Title and author for a UPC-A (plus any add-on) printed on a book.

    A volunteer's mapping of the full code wins; else the 12-digit UPC is
    mapped to an ISBN from the ``upcs`` cache or by UPCitemdb, cached under
    the 12 digits (see ``upc_isbn``). Then the book is the normal ISBN
    lookup. Both 404s say why in ``reason``: ``no_isbn`` (no ISBN known for
    the UPC; cached for 30 days) or ``no_book`` (the UPC maps to ``isbn``,
    which no provider knows). UPCitemdb rate-limited or down is a 503, not
    cached.
    """
    check_upc_code(code)
    row = await upc_isbn(session, code)
    if isinstance(row, JSONResponse):
        return row
    assert row.isbn is not None
    try:
        book = await book_for_isbn(session, row.isbn)
    except HTTPException as error:
        if error.status_code == status.HTTP_404_NOT_FOUND:
            return no_book(row.isbn)
        raise
    return schemas.UpcBook(**book.model_dump(), upc_source=row.source)


@app.put("/books/upc/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def put_book_upc(
    code: str,
    mapping: schemas.UpcMapping,
    session: AsyncSession = Depends(get_session),
):
    """Record the ISBN a volunteer found for a UPC the lookup didn't know.

    Stored under the full code, which must include its add-on: on older
    mass-market paperbacks the bare 12-digit UPC may identify only the
    publisher and price, shared by many titles. Only the full code's own
    mapping is checked, so the volunteer's ISBN overrides whatever the
    12-digit UPC maps to; a full code already mapped to a different ISBN is
    a 409 and keeps its mapping.
    """
    check_upc_code(code)
    if not upcs.has_add_on(code):
        raise HTTPException(
            status_code=422,
            detail="A UPC without its 2- or 5-digit add-on can't be mapped to "
            "one ISBN.",
        )
    isbn = books.normalize_isbn(mapping.isbn)
    if isbn is None:
        raise HTTPException(status_code=422, detail="Not a valid ISBN.")

    row = await write_upc(session, code, isbn, "volunteer")
    if row is None or row.isbn != isbn:
        mapped = f"ISBN {row.isbn}" if row and row.isbn else "another ISBN"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This UPC is already mapped to {mapped}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
