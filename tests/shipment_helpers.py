"""Seeding helpers for the shipment tracking tests. All data is invented."""

import datetime
import itertools
from typing import Any, Optional

from sqlalchemy.orm import Session

from ibp import models

UTC = datetime.timezone.utc
_ids = itertools.count(1)


def utc(text: str) -> datetime.datetime:
    """An aware UTC datetime from ``YYYY-MM-DDTHH:MM``."""
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def add_unit(
    session: Session, name: str, jurisdiction: str = "Texas", **columns: Any
) -> models.Unit:
    """Add a unit at an invented address (``columns`` override its values;
    the shipping method defaults to Box)."""
    values: dict[str, Any] = {
        "street1": "100 Example Rd",
        "street2": None,
        "city": "Exampleton",
        "zipcode": "77000",
        "state": "TX",
        "shipping_method": "Box",
        **columns,
    }
    unit = models.Unit(jurisdiction=jurisdiction, name=name, **values)
    session.add(unit)
    return unit


def add_inmate(
    session: Session,
    inmate_id: int,
    unit_name: Optional[str] = None,
    jurisdiction: str = "Texas",
) -> models.Inmate:
    """Add an inmate with an invented name."""
    inmate = models.Inmate(
        jurisdiction=jurisdiction,
        id=inmate_id,
        first_name="TEST",
        last_name="PERSON",
        unit_name=unit_name,
    )
    session.add(inmate)
    return inmate


def add_request(
    session: Session, inmate_id: int, index: int, processed: str, **options: str
) -> models.Request:
    """Add a request processed on ``processed`` (``YYYY-MM-DD``); ``options``
    may set ``jurisdiction`` (Texas) and ``action`` (Filled)."""
    date = datetime.date.fromisoformat(processed)
    request = models.Request(
        inmate_jurisdiction=options.get("jurisdiction", "Texas"),
        inmate_id=inmate_id,
        index=index,
        date_processed=date,
        date_postmarked=date - datetime.timedelta(days=7),
        action=options.get("action", "Filled"),
    )
    session.add(request)
    return request


def add_shipment(
    session: Session,
    purchased: str,
    kind: str = "individual",
    events: tuple[tuple[str, str], ...] = (),
    **columns: Any,
) -> models.Shipment:
    """Add a stored label bought at ``purchased`` (UTC) with ``events``
    given as (UTC time, status)."""
    number = next(_ids)
    shipment = models.Shipment(
        easypost_id=f"shp_test{number}",
        tracking_code=f"9400{number:018d}",
        tracker_id=f"trk_test{number}",
        purchased_at=utc(purchased),
        kind=kind,
        synced_at=utc(purchased),
        events=[
            models.TrackingEvent(occurred_at=utc(when), status=status)
            for when, status in events
        ],
        **columns,
    )
    session.add(shipment)
    return shipment


def links(session: Session) -> set[tuple]:
    """Every link as (jurisdiction, inmate id, request index, label id, basis)."""
    return {
        (
            link.inmate_jurisdiction,
            link.inmate_id,
            link.request_index,
            link.shipment.easypost_id,
            link.basis,
        )
        for link in session.query(models.PackageShipment)
    }
