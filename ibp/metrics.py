"""Metrics aggregation for requests."""

# pylint: disable=not-callable

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import String, and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import models, schemas


def get_month_key(date_column, dialect: str):
    """Generate month key expression based on database dialect."""
    if dialect == "postgresql":
        return func.to_char(date_column, "YYYY-MM")
    if dialect == "sqlite":
        return func.strftime("%Y-%m", date_column)
    # Fallback: extract year/month and concatenate
    year = func.extract("year", date_column)
    month = func.extract("month", date_column)
    return func.concat(
        func.cast(year, String), "-", func.lpad(func.cast(month, String), 2, "0")
    )


def calculate_date_range(
    date_range: Optional[str],
) -> tuple[Optional[date], Optional[date]]:
    """Calculate start and end dates from preset."""
    if not date_range or date_range == "all":
        return None, None

    end_date = datetime.now().date()

    if date_range == "6months":
        start_date = end_date - timedelta(days=180)
    elif date_range == "12months":
        start_date = end_date - timedelta(days=365)
    else:
        return None, None

    return start_date, end_date


# pylint: disable=too-many-arguments, too-many-positional-arguments
async def get_request_metrics(
    session: AsyncSession,
    unit_jurisdiction: Optional[str] = None,
    unit_name: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[schemas.MonthlyMetricPoint]:
    """Get request counts grouped by month."""
    # Get database dialect for proper date function
    dialect = session.bind.dialect.name
    month_key = get_month_key(models.Request.date_postmarked, dialect)

    # Build query with conditional aggregations
    query = select(
        month_key.label("month"),
        func.sum(case((models.Request.action == "Filled", 1), else_=0)).label(
            "filled_count"
        ),
        func.sum(case((models.Request.action == "Tossed", 1), else_=0)).label(
            "tossed_count"
        ),
        func.count().label("total_count"),
    ).select_from(models.Request)

    # Join to Inmate if filtering by unit
    if unit_jurisdiction or unit_name:
        query = query.join(
            models.Inmate,
            and_(
                models.Request.inmate_jurisdiction == models.Inmate.jurisdiction,
                models.Request.inmate_id == models.Inmate.id,
            ),
        )

    # Apply filters
    filters = []

    if unit_jurisdiction and unit_name:
        filters.append(models.Inmate.unit_name == unit_name)
        filters.append(models.Inmate.jurisdiction == unit_jurisdiction)

    if jurisdiction:
        filters.append(models.Request.inmate_jurisdiction == jurisdiction)

    if action:
        filters.append(models.Request.action == action)

    if start_date:
        filters.append(models.Request.date_postmarked >= start_date)

    if end_date:
        filters.append(models.Request.date_postmarked <= end_date)

    if filters:
        query = query.where(and_(*filters))

    # Group by month and order
    query = query.group_by(month_key).order_by(month_key)

    result = await session.execute(query)
    rows = result.all()

    return [
        schemas.MonthlyMetricPoint(
            month=row.month,
            filled_count=row.filled_count,
            tossed_count=row.tossed_count,
            total_count=row.total_count,
        )
        for row in rows
    ]
