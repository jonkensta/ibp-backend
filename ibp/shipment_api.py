"""Read-only view of the postage labels linked to a request.

The labels, their tracking events and the links are written only by the
shipment sync (``python -m ibp.shipment_sync``); see ``ibp/shipment_links.py``
for how a request is linked to a label.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from . import models, schemas
from .api import get_session
from .base import app
from .request_api import load_request


@app.get("/requests/{code}/shipments", response_model=list[schemas.RequestShipment])
async def get_request_shipments(
    code: str, session: AsyncSession = Depends(get_session)
):
    """List the labels a request could have shipped under, oldest first.

    Each has its tracking code, purchase time (UTC), kind, the basis of the
    link, the tracker's current status and every tracking event. Empty until
    the sync has linked a label. An unknown or malformed code is a 404.
    """
    request = await load_request(session, code)
    links = (
        await session.scalars(
            select(models.PackageShipment)
            .join(models.PackageShipment.shipment)
            .where(
                models.PackageShipment.inmate_jurisdiction
                == request.inmate_jurisdiction,
                models.PackageShipment.inmate_id == request.inmate_id,
                models.PackageShipment.request_index == request.index,
            )
            .options(
                selectinload(models.PackageShipment.shipment).selectinload(
                    models.Shipment.events
                )
            )
            .order_by(models.Shipment.purchased_at, models.Shipment.id)
        )
    ).all()
    return [
        schemas.RequestShipment(
            tracking_code=link.shipment.tracking_code,
            purchased_at=link.shipment.purchased_at,
            kind=schemas.ShipmentKindEnum(link.shipment.kind),
            basis=schemas.LinkBasisEnum(link.basis),
            tracker_status=link.shipment.tracker_status,
            events=[
                schemas.TrackingEvent.model_validate(event)
                for event in link.shipment.events
            ],
        )
        for link in links
    ]
