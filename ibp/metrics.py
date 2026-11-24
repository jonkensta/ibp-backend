"""Metrics aggregation for requests."""

# pylint: disable=not-callable

import math
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import String, and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import clock, models, schemas
from .delivery_latency import is_provisional, month_end, percentile

# A postmark month is provisional until it is ``settle_days`` old: a request
# is only recorded when its letter is answered, so a recent month's counts
# are still rising. The settle time is the ``SETTLE_PERCENTILE`` of the days
# from postmark to processing, over the requests postmarked in the
# ``SETTLE_SAMPLE`` days before the months that ended more than
# ``SETTLE_AGE`` days ago; ``DEFAULT_SETTLE_DAYS`` when there are fewer than
# ``SETTLE_MIN_REQUESTS`` of them. Waits outside 0..``MAX_WAIT`` days are
# mistyped postmarks and are left out.
SETTLE_PERCENTILE = 0.95
SETTLE_AGE = 120
SETTLE_SAMPLE = 365
SETTLE_MIN_REQUESTS = 50
DEFAULT_SETTLE_DAYS = 75
MAX_WAIT = 365


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
    """Calculate start and end dates from preset.

    The range ends today in the configured time zone (``clock.today``).
    """
    if not date_range or date_range == "all":
        return None, None

    end_date = clock.today()

    if date_range == "6months":
        start_date = end_date - timedelta(days=180)
    elif date_range == "12months":
        start_date = end_date - timedelta(days=365)
    else:
        return None, None

    return start_date, end_date


def resolve_date_range(
    date_range: Optional[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> tuple[Optional[date], Optional[date]]:
    """The range a metrics endpoint uses: an explicit ``start_date`` or
    ``end_date`` wins for its bound, and the ``date_range`` preset only fills
    a bound not given (``all`` and unknown presets fill none)."""
    preset_start, preset_end = calculate_date_range(date_range)
    return (
        start_date if start_date is not None else preset_start,
        end_date if end_date is not None else preset_end,
    )


async def request_settle_days(session: AsyncSession, today: date) -> int:
    """How long a postmark month's request counts take to settle, in days
    (see ``SETTLE_PERCENTILE``). It ignores the page's filters, so a month is
    provisional or not whatever is being shown."""
    newest = today - timedelta(days=SETTLE_AGE)
    rows = await session.execute(
        select(models.Request.date_postmarked, models.Request.date_processed).where(
            models.Request.date_postmarked >= newest - timedelta(days=SETTLE_SAMPLE),
            models.Request.date_postmarked < newest,
        )
    )
    waits = [
        (processed - postmarked).days
        for postmarked, processed in rows
        if (today - month_end(postmarked.strftime("%Y-%m"))).days > SETTLE_AGE
        and 0 <= (processed - postmarked).days <= MAX_WAIT
    ]
    if len(waits) < SETTLE_MIN_REQUESTS:
        return DEFAULT_SETTLE_DAYS
    return math.ceil(percentile(waits, SETTLE_PERCENTILE))


# pylint: disable=too-many-arguments, too-many-positional-arguments
async def get_request_metrics(
    session: AsyncSession,
    unit_jurisdiction: Optional[str] = None,
    unit_name: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    settle_days: int = DEFAULT_SETTLE_DAYS,
) -> list[schemas.MonthlyMetricPoint]:
    """Get request counts grouped by postmark month. A month that ended
    fewer than ``settle_days`` ago is marked provisional."""
    today = clock.today()
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
            provisional=is_provisional(row.month, today, settle_days),
        )
        for row in rows
    ]
