"""Link requests to the postage labels they could have shipped under.

``package_shipments`` is derived: ``rebuild_links`` deletes every row and
builds them again from ``shipments``, ``tracking_events`` and the request
data, so it can be rerun at any time (``python -m ibp.shipment_sync
--rebuild-links``) and gives the same rows for the same data. The rules,
each named by the link's ``basis``:

- ``reference``: a label whose reference tag names a request
  (``ibp1:req:<code>``) links to that request, if IBP has it.
- ``inmate_number``: an untagged individual label, whose addressee line
  ends in an inmate number, links to that inmate's latest Filled request
  processed on the label's purchase date (local) or up to ``LINK_WINDOW``
  before it. Texas and Federal numbers look alike, so the label's
  destination unit decides the jurisdiction; with no destination, a number
  that both jurisdictions could match links to neither.
- ``unit_session``: a session is every bulk box to one place on one local
  date that USPS has scanned, or that was bought less than ``FRESH_BOX``
  (7 days) ago and so may not have its first scan yet. A place is a unit,
  or the units that are one facility under several provider codes
  (``ibp.unit_places.Places``: same jurisdiction, display name and 5-digit
  ZIP): a box stored under any of them serves the inmates under every one,
  and boxes stored under different units of one place on one date are one
  session. A box to a unit IBP doesn't have makes no session. A Filled
  request with no label of its own (no ``reference`` or ``inmate_number``
  link) links to every box of the first session to its unit's place at
  least ``BULK_LEAD`` (a day) after the request's processed date, so a
  request processed on a session's date waits for the next one, if that
  session is within ``BULK_LINK_WINDOW`` of it. A request's unit is the one
  it recorded when it was filled (``requests.unit_name``), so a transfer
  afterwards doesn't move it; one that recorded no unit (the inmate's was
  unknown then) links to no box. A request filled before units were
  recorded (``unit_recorded`` false) uses its inmate's unit as it is now.
  The unit's shipping method is not consulted: boxes do go to units marked
  Individual, and a package with its own label is already linked to it.

A label that was never mailed (``never_mailed``) gets no links. Nothing
here works out delivery: the stored events are left for that.
"""

import bisect
import datetime
from collections import defaultdict
from typing import Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from . import clock, models
from .unit_places import Places, UnitKey

# How long before an untagged individual label's purchase date its request
# may have been processed.
LINK_WINDOW = datetime.timedelta(days=60)
# How long after a request's processed date its bulk session may be at most.
BULK_LINK_WINDOW = datetime.timedelta(days=28)
# How long after a request's processed date its bulk session must be at
# least: a request processed on a session's date waits for the next one. A
# one-day lead fit a year of box weights better than a same-day rule.
BULK_LEAD = datetime.timedelta(days=1)
# Statuses that are no evidence USPS has handled a package.
UNSCANNED = frozenset({"unknown", "pre_transit", "error", "cancelled"})
# An unscanned bulk box bought this recently still counts toward a session.
FRESH_BOX = datetime.timedelta(days=7)
# A refund asked for (and, at USPS, granted unless the label was used).
REFUND_REQUESTED = frozenset({"submitted", "refunded"})

LinkKey = tuple[str, int, int, int]


def is_scanned(shipment: models.Shipment) -> bool:
    """Whether USPS ever handled the label: any status but ``UNSCANNED``.

    The shipment's events must be loaded.
    """
    statuses = [shipment.tracker_status] + [e.status for e in shipment.events]
    return any(status and status not in UNSCANNED for status in statuses)


def never_mailed(shipment: models.Shipment) -> bool:
    """Whether the label was refunded (or a refund asked for) and never scanned.

    Such a label was never mailed. One that was refunded but scanned did
    travel, and counts as mailed.
    """
    return shipment.refund_status in REFUND_REQUESTED and not is_scanned(shipment)


def local_date(
    moment: datetime.datetime, timezone: ZoneInfo = clock.TIMEZONE
) -> datetime.date:
    """The date of ``moment`` in the server's time zone."""
    return moment.astimezone(timezone).date()


def _reference_links(
    session: Session, shipments: list[models.Shipment], links: dict[LinkKey, str]
) -> None:
    for shipment in shipments:
        if (
            shipment.request_jurisdiction is None
            or shipment.request_inmate_id is None
            or shipment.request_index is None
        ):
            continue
        key = (
            shipment.request_jurisdiction,
            shipment.request_inmate_id,
            shipment.request_index,
        )
        if session.get(models.Request, key) is not None:
            links[(*key, shipment.id)] = "reference"


def _inmate_number_links(
    session: Session, shipments: list[models.Shipment], links: dict[LinkKey, str]
) -> None:
    for shipment in shipments:
        if (
            shipment.kind != "individual"
            or shipment.request_inmate_id is not None
            or not shipment.inmate_number
        ):
            continue
        purchased = local_date(shipment.purchased_at)
        query = (
            sa.select(
                models.Request.inmate_jurisdiction,
                models.Request.inmate_id,
                models.Request.index,
            )
            .where(
                models.Request.inmate_id == int(shipment.inmate_number),
                models.Request.action == "Filled",
                models.Request.date_processed <= purchased,
                models.Request.date_processed >= purchased - LINK_WINDOW,
            )
            .order_by(
                models.Request.date_processed.desc(),
                models.Request.index.desc(),
                models.Request.inmate_jurisdiction,
            )
        )
        # Naming the jurisdictions lets the request key's index be used.
        jurisdictions = (
            [shipment.unit_jurisdiction]
            if shipment.unit_jurisdiction is not None
            else ["Texas", "Federal"]
        )
        query = query.where(models.Request.inmate_jurisdiction.in_(jurisdictions))
        candidates = session.execute(query).all()
        if not candidates or len({row[0] for row in candidates}) > 1:
            continue  # nothing qualifies, or Texas and Federal both could
        jurisdiction, inmate_id, index = candidates[0]
        links[(jurisdiction, inmate_id, index, shipment.id)] = "inmate_number"


def _bulk_sessions(
    shipments: list[models.Shipment], places: Places, now: datetime.datetime
) -> dict[UnitKey, dict[datetime.date, list[int]]]:
    """Group bulk boxes by place (its representative), then by local date:
    place -> date -> box ids.

    Only boxes USPS has scanned, or bought less than ``FRESH_BOX`` ago,
    count: an older box never scanned was likely never mailed. A box to a
    unit IBP doesn't have is left out.
    """
    sessions: dict[UnitKey, dict[datetime.date, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for shipment in shipments:
        if (
            shipment.kind == "bulk"
            and shipment.unit_jurisdiction is not None
            and shipment.unit_name is not None
            and (is_scanned(shipment) or now - shipment.purchased_at < FRESH_BOX)
        ):
            place = places.representative(
                (shipment.unit_jurisdiction, shipment.unit_name)
            )
            if place is not None:
                sessions[place][local_date(shipment.purchased_at)].append(shipment.id)
    return sessions


def request_unit() -> sa.ColumnElement:
    """A request's unit for linking: the one it recorded when filled
    (possibly none, which matches no unit) if it recorded one, else its
    inmate's current unit (a request filled before units were recorded).
    The query must join the request's inmate."""
    return sa.case(
        (models.Request.unit_recorded, models.Request.unit_name),
        else_=models.Inmate.unit_name,
    )


def _place_requests(
    session: Session,
    units: frozenset[UnitKey],
    first: datetime.date,
    last: datetime.date,
) -> list[sa.Row]:
    """Filled requests to a place's ``units`` that a session from ``first``
    to ``last`` could link: (jurisdiction, inmate id, index, processed date).

    A request's unit is ``request_unit``.
    """
    (jurisdiction,) = {unit[0] for unit in units}
    unit_names = sorted(unit[1] for unit in units)
    return list(
        session.execute(
            sa.select(
                models.Request.inmate_jurisdiction,
                models.Request.inmate_id,
                models.Request.index,
                models.Request.date_processed,
            )
            .join(
                models.Inmate,
                sa.and_(
                    models.Inmate.jurisdiction == models.Request.inmate_jurisdiction,
                    models.Inmate.id == models.Request.inmate_id,
                ),
            )
            .where(
                models.Inmate.jurisdiction == jurisdiction,
                request_unit().in_(unit_names),
                models.Request.action == "Filled",
                models.Request.date_processed >= first - BULK_LINK_WINDOW,
                models.Request.date_processed <= last - BULK_LEAD,
            )
        ).all()
    )


def _unit_session_links(
    session: Session,
    shipments: list[models.Shipment],
    links: dict[LinkKey, str],
    now: datetime.datetime,
) -> None:
    # Requests already linked to a label of their own did not ride in a box.
    labelled = {key[:3] for key in links}
    places = Places(session.scalars(sa.select(models.Unit)).all())
    for place, by_date in sorted(_bulk_sessions(shipments, places, now).items()):
        dates = sorted(by_date)
        for jurisdiction, inmate_id, index, processed in _place_requests(
            session, places.members(place), dates[0], dates[-1]
        ):
            if (jurisdiction, inmate_id, index) in labelled:
                continue
            position = bisect.bisect_left(dates, processed + BULK_LEAD)
            if position == len(dates) or dates[position] - processed > BULK_LINK_WINDOW:
                continue
            for shipment_id in by_date[dates[position]]:
                links[(jurisdiction, inmate_id, index, shipment_id)] = "unit_session"


def rebuild_links(session: Session, now: Optional[datetime.datetime] = None) -> int:
    """Delete and rebuild every ``package_shipments`` row; return how many.

    Runs in the caller's transaction. ``now`` (the current time by default)
    decides which unscanned boxes are still fresh.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    session.execute(sa.delete(models.PackageShipment))
    shipments = [
        shipment
        for shipment in session.scalars(
            sa.select(models.Shipment)
            .options(selectinload(models.Shipment.events))
            .order_by(models.Shipment.id)
        ).all()
        if not never_mailed(shipment)
    ]
    links: dict[LinkKey, str] = {}
    _reference_links(session, shipments, links)
    _inmate_number_links(session, shipments, links)
    _unit_session_links(session, shipments, links, now)
    session.add_all(
        models.PackageShipment(
            inmate_jurisdiction=jurisdiction,
            inmate_id=inmate_id,
            request_index=index,
            shipment_id=shipment_id,
            basis=basis,
        )
        for (jurisdiction, inmate_id, index, shipment_id), basis in sorted(
            links.items()
        )
    )
    session.flush()
    return len(links)
