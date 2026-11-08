"""ISBN and UPC lookup endpoints (see ``ibp.books`` and ``ibp.upcs``)."""

import datetime

from fastapi import Depends, HTTPException, Response, status
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


def upc_not_found() -> HTTPException:
    """The 404 for a UPC with no known ISBN."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="No ISBN known for this UPC."
    )


async def store_upc(
    session: AsyncSession,
    row: models.Upc | None,
    code: str,
    isbn: str | None,
    source: str,
) -> bool:
    """Insert or update the ``upcs`` row for ``code``.

    False if another request inserted a row for it first (that row is kept).
    """
    if row is None:
        session.add(models.Upc(upc=code, isbn=isbn, source=source, looked_up_at=now()))
    else:
        row.isbn, row.source, row.looked_up_at = isbn, source, now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


@app.get("/books/upc/{code}", response_model=schemas.UpcBook)
async def get_book_by_upc(code: str, session: AsyncSession = Depends(get_session)):
    """Title and author for a UPC-A (plus any add-on) printed on a book.

    The UPC is mapped to an ISBN from the ``upcs`` cache (keyed by the full
    code, add-on included), else by UPCitemdb (asked with the 12-digit UPC);
    then the book is the normal ISBN lookup. A UPC no one maps is a 404, and
    is cached for 30 days; UPCitemdb being rate-limited or down is a 503,
    not cached.
    """
    check_upc_code(code)
    row = await session.get(models.Upc, code)
    if row is not None and row.isbn is not None:
        isbn, source = row.isbn, row.source
    elif row is not None and now() - row.looked_up_at < UPC_MISS_TTL:
        raise upc_not_found()
    else:
        try:
            found = await upcs.upcitemdb(code[:12])
        except upcs.UpcServiceUnavailable as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error
        source = "upcitemdb"
        await store_upc(session, row, code, found, source)
        if found is None:
            raise upc_not_found()
        isbn = found

    book = await book_for_isbn(session, isbn)
    return schemas.UpcBook(**book.model_dump(), upc_source=source)


@app.put("/books/upc/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def put_book_upc(
    code: str,
    mapping: schemas.UpcMapping,
    session: AsyncSession = Depends(get_session),
):
    """Record the ISBN a volunteer found for a UPC the lookup didn't know.

    Only a code with its add-on is accepted: on older mass-market paperbacks
    the bare 12-digit UPC may identify only the publisher and price, shared
    by many titles. A UPC already mapped to a different ISBN is a 409 and
    keeps its mapping.
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

    def conflict(existing: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This UPC is already mapped to ISBN {existing}.",
        )

    row = await session.get(models.Upc, code)
    if row is not None and row.isbn is not None:
        if row.isbn != isbn:
            raise conflict(row.isbn)
    elif not await store_upc(session, row, code, isbn, "volunteer"):
        # Another request stored this UPC first; check it against ours.
        row = await session.get(models.Upc, code)
        if row is not None and row.isbn != isbn:
            raise conflict(row.isbn or "none")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
