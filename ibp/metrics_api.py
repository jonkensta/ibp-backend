"""Metrics endpoints kept out of ``ibp/api.py``.

``GET /metrics/requests`` is still in ``api.py``; the date range options
here follow it.
"""

import datetime
from typing import Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from . import delivery_latency, metrics, schemas
from .api import get_session
from .base import app


@app.get("/metrics/delivery-latency", response_model=schemas.DeliveryLatencyResponse)
async def get_metrics_delivery_latency(
    session: AsyncSession = Depends(get_session),
    method: schemas.DeliveryMethodEnum = schemas.DeliveryMethodEnum.ALL,
    date_range: Optional[str] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
):
    """Postmark-to-delivery latency of Filled requests, by postmark month.

    ``method`` picks the links counted: ``all``, ``individual`` (a request's
    own label) or ``bulk`` (a box to the unit). The date range filters the
    postmark, as for ``/metrics/requests``: an explicit ``start_date`` or
    ``end_date`` wins for its bound, and the ``date_range`` preset fills a
    bound not given. ``filters_applied`` reports the bounds used. See ``ibp/delivery_latency.py``
    for the definitions. ``settle_days`` is how long a postmark month takes
    to settle; each month says whether it is still ``provisional``. With no
    start to the range, the months before the first with a linked request
    are left out.
    """
    # Explicit bounds win; the preset fills only a bound not given.
    start_date, end_date = metrics.resolve_date_range(date_range, start_date, end_date)

    # With no start, given or from the preset, begin where shipment data
    # does, not decades back.
    summary = await delivery_latency.get_delivery_latency(
        session,
        method=method,
        start_date=start_date,
        end_date=end_date,
        from_first_linked=start_date is None,
    )
    return schemas.DeliveryLatencyResponse(
        data=summary.months,
        settle_days=summary.settle_days,
        filters_applied={
            "method": method.value,
            "date_range": date_range,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
