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
    own label) or ``bulk`` (a box to the unit). The date range (a
    ``date_range`` preset, or ``start_date`` and ``end_date``) filters the
    postmark, as for ``/metrics/requests``. See ``ibp/delivery_latency.py``
    for the definitions.
    """
    if date_range and not (start_date and end_date):
        start_date, end_date = metrics.calculate_date_range(date_range)

    data = await delivery_latency.get_delivery_latency(
        session, method=method, start_date=start_date, end_date=end_date
    )
    return schemas.DeliveryLatencyResponse(
        data=data,
        filters_applied={
            "method": method.value,
            "date_range": date_range,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
