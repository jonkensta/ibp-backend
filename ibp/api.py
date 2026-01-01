"""IBP API views."""

import datetime
import io
import itertools
import logging
from typing import Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Query, Response, status
from nameparser import HumanName  # type: ignore[import]
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import base, db, metrics, models, schemas, warnings
from .base import app
from .labels import render_request_label
from .upsert import inmates_by_inmate_id as upsert_inmates_by_inmate_id
from .upsert import inmates_by_name as upsert_inmates_by_name

logger = logging.getLogger(__name__)


async def get_session():
    """Dependency that provides an asynchronous SQLAlchemy session."""
    async with db.async_session() as session:
        yield session


@app.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)):
    """Health check endpoint that verifies database connectivity."""
    try:
        # Quick database check - just verify we can execute a query
        await session.execute(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        logger.error("Health check failed: %s", str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from e


async def query_inmates_by_inmate_id(session: AsyncSession, inmate_id: int):
    """Query inmates table by inmate ID."""
    return (
        (
            await session.execute(
                select(models.Inmate).where(models.Inmate.id == inmate_id)
            )
        )
        .scalars()
        .all()
    )


async def query_inmates_by_name(session: AsyncSession, first_name: str, last_name: str):
    """Query inmates table by name."""
    lower = sqlalchemy.func.lower
    return (
        (
            await session.execute(
                select(models.Inmate)
                .where(lower(models.Inmate.last_name) == lower(last_name))
                .where(models.Inmate.first_name.ilike(first_name + "%"))
            )
        )
        .scalars()
        .all()
    )


@app.on_event("startup")
def configure_logging():
    """Configure logging."""
    handlers = list(base.build_log_handlers())
    base.configure_root_logger(handlers)
    base.configure_external_loggers(handlers)
    logger.info("Starting IBP Application")


@app.get("/inmates", response_model=schemas.InmateSearchResults)
async def search_inmates(
    session: AsyncSession = Depends(get_session),
    query: str = Query(..., description="Inmate name or ID."),
):
    """Perform an inmate search by name or inmate ID."""

    errors = []
    inmates = []

    try:
        inmate_id = int(query.replace("-", ""))

    except ValueError:
        name = HumanName(query)
        first: str = name.first
        last: str = name.last
        if not (first and last):
            logger.debug("Failed to parse query: %s", query)

            # pylint: disable=raise-missing-from
            status_code = status.HTTP_400_BAD_REQUEST
            detail = "Query must be an inmate name or ID."
            raise HTTPException(status_code=status_code, detail=detail)

        logger.debug("querying inmates by name: %s %s", first, last)
        errors = await upsert_inmates_by_name(session, first, last)
        inmates = await query_inmates_by_name(session, first, last)

    else:
        logger.debug("querying inmates by ID: %d", inmate_id)
        errors = await upsert_inmates_by_inmate_id(session, inmate_id)
        inmates = await query_inmates_by_inmate_id(session, inmate_id)

    errors_as_strings = list(map(str, errors))
    return schemas.InmateSearchResults(inmates=inmates, errors=errors_as_strings)


@app.get("/inmates/{jurisdiction}/{inmate_id}", response_model=schemas.Inmate)
async def get_inmate(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Retrieve an inmate by jurisdiction and inmate ID."""
    logger.debug("Loading inmate view for %s inmate #%08d", jurisdiction, inmate_id)

    async with session.begin():
        statement = (
            select(models.Inmate)
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                models.Inmate.id == inmate_id,
            )
            .options(
                selectinload(models.Inmate.unit),
                selectinload(models.Inmate.requests),
                selectinload(models.Inmate.comments),
                selectinload(models.Inmate.lookups),
            )
            .with_for_update()
        )

        inmate = (await session.execute(statement)).scalar_one_or_none()

        if inmate is None or not inmate.entry_is_fresh():
            logger.debug(
                "Inmate not found or not fresh, querying providers for %s inmate #%08d",
                jurisdiction,
                inmate_id,
            )
            await upsert_inmates_by_inmate_id(session, inmate_id)
            inmate = (await session.execute(statement)).scalar_one_or_none()

        if inmate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Inmate not found."
            )

        used_indices = [lookup.index for lookup in inmate.lookups]
        next_index = next(
            index for index in itertools.count() if index not in used_indices
        )

        lookup = models.Lookup(
            datetime_created=datetime.datetime.now(datetime.timezone.utc),
            index=next_index,
        )
        inmate.lookups.append(lookup)
        inmate.lookups.sort(key=lambda lookup: lookup.datetime_created)

        session.add(lookup)
        del inmate.lookups[:-3]

    await session.refresh(inmate)
    return inmate


@app.get(
    "/inmates/{jurisdiction}/{inmate_id}/warnings",
    response_model=schemas.InmateWarnings,
)
async def get_inmate_warnings(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get warnings about an inmate's data or status."""
    inmate = (
        await session.execute(
            select(models.Inmate)
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                models.Inmate.id == inmate_id,
            )
            .options(selectinload(models.Inmate.requests))
        )
    ).scalar_one_or_none()

    if inmate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inmate not found."
        )

    warning_dict = warnings.inmate(inmate)
    return schemas.InmateWarnings(**warning_dict)


@app.post(
    "/inmates/{jurisdiction}/{inmate_id}/requests/validate",
    response_model=schemas.RequestValidationWarnings,
)
async def validate_request(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_data: schemas.RequestCreate,
    session: AsyncSession = Depends(get_session),
):
    """Validate a request before creation, returning all warnings."""
    inmate = (
        await session.execute(
            select(models.Inmate)
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                models.Inmate.id == inmate_id,
            )
            .options(selectinload(models.Inmate.requests))
        )
    ).scalar_one_or_none()

    if inmate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inmate not found."
        )

    inmate_warnings_dict = warnings.inmate(inmate)
    request_warnings_dict = warnings.request(inmate, request_data.date_postmarked)

    return schemas.RequestValidationWarnings(
        **inmate_warnings_dict, **request_warnings_dict
    )


@app.post(
    "/inmates/{jurisdiction}/{inmate_id}/requests", response_model=schemas.Request
)
async def add_request(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_data: schemas.RequestCreate,
    session: AsyncSession = Depends(get_session),
):
    """Add a new request for an inmate."""
    async with session.begin():
        statement = (
            select(models.Inmate)
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                models.Inmate.id == inmate_id,
            )
            .with_for_update()
        )

        inmate = (await session.execute(statement)).scalar_one_or_none()

        if inmate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Inmate not found."
            )

        used_indices = (
            (
                await session.execute(
                    select(models.Request.index).where(
                        models.Request.inmate_jurisdiction == jurisdiction,
                        models.Request.inmate_id == inmate_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        next_index = next(
            index for index in itertools.count() if index not in used_indices
        )

        request = models.Request(
            **request_data.model_dump(),
            inmate_jurisdiction=jurisdiction,
            inmate_id=inmate_id,
            index=next_index,
        )
        session.add(request)

    await session.refresh(request)

    logger.debug(
        "adding request #%d with %s postmark for %s inmate #%08d",
        request.index,
        request.date_postmarked,
        jurisdiction,
        inmate_id,
    )

    return request


@app.delete(
    "/inmates/{jurisdiction}/{inmate_id}/requests/{request_index}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_request(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_index: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a request."""
    async with session.begin():
        request = (
            await session.execute(
                select(models.Request).where(
                    models.Request.inmate_jurisdiction == jurisdiction,
                    models.Request.inmate_id == inmate_id,
                    models.Request.index == request_index,
                )
            )
        ).scalar_one_or_none()

        if request is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Request not found."
            )

        logger.debug(
            "Deleting request #%d for inmate %s %d",
            request_index,
            jurisdiction,
            inmate_id,
        )
        await session.delete(request)


@app.get(
    "/inmates/{jurisdiction}/{inmate_id}/requests/{request_index}/label",
    response_class=Response,
)
async def get_request_label(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    request_index: int,
    session: AsyncSession = Depends(get_session),
):
    """Return a PNG label image for a request."""
    request = (
        await session.execute(
            select(models.Request)
            .options(
                selectinload(models.Request.inmate).selectinload(models.Inmate.unit)
            )
            .where(
                models.Request.inmate_jurisdiction == jurisdiction,
                models.Request.inmate_id == inmate_id,
                models.Request.index == request_index,
            )
        )
    ).scalar_one_or_none()

    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found."
        )

    image = render_request_label(request)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.post(
    "/inmates/{jurisdiction}/{inmate_id}/comments", response_model=schemas.Comment
)
async def add_comment(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    comment_data: schemas.CommentCreate,
    session: AsyncSession = Depends(get_session),
):
    """Add a new comment for a specific inmate."""
    async with session.begin():
        statement = (
            select(models.Inmate)
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                models.Inmate.id == inmate_id,
            )
            .with_for_update()
        )

        inmate = (await session.execute(statement)).scalar_one_or_none()

        if inmate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Inmate not found."
            )

        used_indices = (
            (
                await session.execute(
                    select(models.Comment.index).where(
                        models.Comment.inmate_jurisdiction == jurisdiction,
                        models.Comment.inmate_id == inmate_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        next_index = next(
            index for index in itertools.count() if index not in used_indices
        )

        comment = models.Comment(
            **comment_data.model_dump(),
            datetime_created=datetime.datetime.now(datetime.timezone.utc),
            inmate_jurisdiction=jurisdiction,
            inmate_id=inmate_id,
            index=next_index,
        )
        session.add(comment)

    await session.refresh(comment)

    logger.debug(
        "adding comment #%d for %s inmate #%08d",
        comment.index,
        jurisdiction,
        inmate_id,
    )

    return comment


@app.delete(
    "/inmates/{jurisdiction}/{inmate_id}/comments/{comment_index}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_comment(
    jurisdiction: schemas.JurisdictionEnum,
    inmate_id: int,
    comment_index: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a comment."""
    async with session.begin():
        comment = (
            await session.execute(
                select(models.Comment).where(
                    models.Comment.inmate_jurisdiction == jurisdiction,
                    models.Comment.inmate_id == inmate_id,
                    models.Comment.index == comment_index,
                )
            )
        ).scalar_one_or_none()

        if comment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found."
            )

        logger.debug(
            "deleting comment #%d for inmate %s %d",
            comment_index,
            jurisdiction,
            inmate_id,
        )

        await session.delete(comment)


@app.get("/units", response_model=list[schemas.Unit])
async def get_all_units(session: AsyncSession = Depends(get_session)):
    """Retrieve a list of all prison units."""
    logger.debug("retrieving all units")
    return (await session.execute(select(models.Unit))).scalars().all()


@app.get("/units/{jurisdiction}/{name}", response_model=schemas.Unit)
async def get_unit(
    jurisdiction: schemas.JurisdictionEnum,
    name: str,
    session: AsyncSession = Depends(get_session),
):
    """Retrieve a single unit by its ID."""

    logger.debug("retrieving unit with jurisdiction=%s, name=%s", jurisdiction, name)

    unit = (
        await session.execute(
            select(models.Unit).where(
                models.Unit.jurisdiction == jurisdiction, models.Unit.name == name
            )
        )
    ).scalar_one_or_none()

    if unit is None:
        status_code = status.HTTP_404_NOT_FOUND
        detail = "Unit not found."
        raise HTTPException(status_code=status_code, detail=detail)

    return unit


@app.put("/units/{jurisdiction}/{name}", response_model=schemas.Unit)
async def update_unit(
    jurisdiction: schemas.JurisdictionEnum,
    name: str,
    unit_data: schemas.UnitUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Update an existing unit."""
    logger.debug("updating unit with jurisdiction=%s, name=%s", jurisdiction, name)

    async with session.begin():
        unit = (
            await session.execute(
                select(models.Unit).where(
                    models.Unit.jurisdiction == jurisdiction, models.Unit.name == name
                )
            )
        ).scalar_one_or_none()

        if unit is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unit not found."
            )

        unit.update_from_kwargs(**unit_data.model_dump(exclude_unset=True))

    await session.refresh(unit)

    logger.debug("updated %s %s unit", jurisdiction, name)
    return unit


@app.get("/metrics/requests", response_model=schemas.RequestMetricsResponse)
# pylint: disable=too-many-arguments, too-many-positional-arguments
async def get_metrics_requests(
    session: AsyncSession = Depends(get_session),
    unit_jurisdiction: Optional[schemas.JurisdictionEnum] = None,
    unit_name: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    action: Optional[str] = None,
    date_range: Optional[str] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
):
    """Get request metrics aggregated by month."""
    logger.debug("Fetching request metrics")

    # Calculate date range from preset if provided
    if date_range and not (start_date and end_date):
        start_date, end_date = metrics.calculate_date_range(date_range)

    data = await metrics.get_request_metrics(
        session=session,
        unit_jurisdiction=unit_jurisdiction,
        unit_name=unit_name,
        jurisdiction=jurisdiction if jurisdiction != "All" else None,
        action=action if action != "All" else None,
        start_date=start_date,
        end_date=end_date,
    )

    return schemas.RequestMetricsResponse(
        data=data,
        filters_applied={
            "unit_jurisdiction": unit_jurisdiction,
            "unit_name": unit_name,
            "jurisdiction": jurisdiction,
            "action": action,
            "date_range": date_range,
        },
    )
