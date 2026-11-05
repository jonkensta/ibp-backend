"""ISBN lookup endpoint (see ``ibp.books``)."""

import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import books, models, schemas
from .api import get_session
from .base import app


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

    cached = await session.get(models.Book, normalized)
    if cached is not None:
        return cached

    result = await books.lookup(normalized)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Book not found."
        )

    book = models.Book(
        isbn=normalized,
        title=result.title,
        author=result.author,
        source=result.source,
        confidence=result.confidence,
        candidates=result.candidates,
        fetched_at=datetime.datetime.now(datetime.timezone.utc),
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
