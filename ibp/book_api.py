"""ISBN and UPC lookup endpoints (see ``ibp.books`` and ``ibp.upcs``)."""

import datetime
from dataclasses import asdict, dataclass
from typing import Optional

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import books, models, schemas, upcs
from .api import get_session
from .base import app

# The only writer of ``upcs`` rows now (older rows may say ``upcitemdb`` or
# ``barcodelookup``, from UPC lookup services since removed).
VOLUNTEER = "volunteer"


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


def check_upc_code(code: str) -> str:
    """``code`` normalized by ``upcs.normalize_code``, or a 422.

    A valid code is a UPC-A with an optional 2- or 5-digit add-on.
    """
    code = upcs.normalize_code(code)
    if not upcs.valid_code(code):
        raise HTTPException(
            status_code=422,
            detail="Not a valid UPC (12 digits, plus an optional 2- or 5-digit "
            "add-on).",
        )
    return code


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


async def write_upc(session: AsyncSession, code: str, isbn: str) -> Optional[UpcRow]:
    """Store a volunteer's ``isbn`` for ``code`` unless ``code`` has a row.

    A row already there, or one another request inserts first, is kept.
    Returns the row as it now stands, so the caller can tell whether its
    ISBN won.
    """
    ours = UpcRow(isbn=isbn, source=VOLUNTEER, looked_up_at=now())
    try:
        await session.execute(sa.insert(models.Upc).values(upc=code, **asdict(ours)))
        await session.commit()
        return ours
    except IntegrityError:
        await session.rollback()
    return await read_upc(session, code)


@app.get(
    "/books/upc/{code}",
    response_model=schemas.UpcBook,
    responses={404: {"model": schemas.UpcNotFound}},
)
async def get_book_by_upc(code: str, session: AsyncSession = Depends(get_session)):
    """Title and author for a UPC-A (plus any add-on) printed on a book.

    Answered only from the ``upcs`` map volunteers fill (see
    ``put_book_upc``), by an exact match on the full code: a bare 12-digit
    UPC is shared by many titles, so a code with an add-on never falls back
    to it. The ISBN found is then the normal ISBN lookup. Both 404s say why
    in ``reason``: ``no_isbn`` (no ISBN known for the code; rows with a NULL
    ISBN are misses left by the removed lookup services) or ``no_book`` (the
    code maps to ``isbn``, which no book provider knows).
    """
    code = check_upc_code(code)
    row = await read_upc(session, code)
    if row is None or row.isbn is None:
        return no_isbn()
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
    """Record the ISBN a volunteer scanned for a UPC the map didn't know.

    The code must include its add-on: the bare 12-digit UPC identifies only
    the publisher and price point, shared by many titles, so mapping it
    would give them all one ISBN. A code already mapped to a different ISBN
    is a 409 and keeps its mapping.
    """
    code = check_upc_code(code)
    if not upcs.has_add_on(code):
        raise HTTPException(
            status_code=422,
            detail="A UPC without its 2- or 5-digit add-on can't be mapped to "
            "one ISBN.",
        )
    isbn = books.normalize_isbn(mapping.isbn)
    if isbn is None:
        raise HTTPException(status_code=422, detail="Not a valid ISBN.")

    row = await write_upc(session, code, isbn)
    if row is None or row.isbn != isbn:
        mapped = f"ISBN {row.isbn}" if row and row.isbn else "another ISBN"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This UPC is already mapped to {mapped}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
