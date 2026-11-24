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
  date that USPS has ever scanned (``is_scanned``), or that was bought less
  than ``FRESH_BOX`` (7 days) ago and so may not have its first scan yet.
  A place is a unit,
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

A label that was never mailed (``never_mailed``: refund asked for and
never scanned, by its permanent ``ever_scanned`` flag or its current
evidence) gets no links. Nothing
here works out delivery: the stored events are left for that.

``rebuild_links`` does all its reading first (``compute_links``: no write,
so on SQLite no write lock, and every place's candidate requests in one
query), then replaces the rows in two statements, so the write lock a
volunteer saving a request could wait on is held only briefly. Since those
reads hold no lock, SQLite's ``data_version`` checks that no other
connection committed while they ran; if one did, the links are computed
again (``REBUILD_ATTEMPTS``), and never published from a mix of two states.
"""

import bisect
import datetime
from collections import defaultdict
from typing import Optional, cast
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


def scanned_status(status: Optional[str]) -> bool:
    """Whether a tracker or event status shows USPS handled a package: any
    status but none or ``UNSCANNED``."""
    return bool(status) and status not in UNSCANNED


def is_scanned(shipment: models.Shipment) -> bool:
    """Whether USPS ever handled the label: its permanent ``ever_scanned``
    flag, or a scanned current status or stored event (for a row the flag
    hasn't caught up with). A later unscanned status never undoes a scan.

    The shipment's events must be loaded.
    """
    statuses = [shipment.tracker_status] + [e.status for e in shipment.events]
    return bool(shipment.ever_scanned) or any(map(scanned_status, statuses))


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


def _session_requests(
    session: Session, first: datetime.date, last: datetime.date
) -> list[sa.Row]:
    """Every Filled request some session from ``first`` to ``last`` could
    link, in one query: (jurisdiction, inmate id, index, processed date,
    unit name as ``request_unit``)."""
    return list(
        session.execute(
            sa.select(
                models.Request.inmate_jurisdiction,
                models.Request.inmate_id,
                models.Request.index,
                models.Request.date_processed,
                request_unit(),
            )
            .join(
                models.Inmate,
                sa.and_(
                    models.Inmate.jurisdiction == models.Request.inmate_jurisdiction,
                    models.Inmate.id == models.Request.inmate_id,
                ),
            )
            .where(
                models.Request.action == "Filled",
                models.Request.date_processed >= first - BULK_LINK_WINDOW,
                models.Request.date_processed <= last - BULK_LEAD,
            )
        ).all()
    )


def _first_session(
    dates: list[datetime.date], processed: datetime.date
) -> Optional[datetime.date]:
    """The first of a place's sorted session ``dates`` a request processed
    on ``processed`` links to, or None."""
    position = bisect.bisect_left(dates, processed + BULK_LEAD)
    if position == len(dates) or dates[position] - processed > BULK_LINK_WINDOW:
        return None
    return dates[position]


def _unit_session_links(
    session: Session,
    shipments: list[models.Shipment],
    links: dict[LinkKey, str],
    now: datetime.datetime,
) -> None:
    places = Places(session.scalars(sa.select(models.Unit)).all())
    sessions = _bulk_sessions(shipments, places, now)
    if not sessions:
        return
    dates = {place: sorted(by_date) for place, by_date in sessions.items()}
    # Requests already linked to a label of their own did not ride in a box.
    labelled = {key[:3] for key in links}
    for row in _session_requests(
        session,
        min(place_dates[0] for place_dates in dates.values()),
        max(place_dates[-1] for place_dates in dates.values()),
    ):
        key = (row[0], row[1], row[2])  # Row.index is a tuple method
        place = places.representative((row[0], row[4]))
        if place not in sessions or key in labelled:
            continue
        date = _first_session(dates[place], row[3])
        for shipment_id in sessions[place].get(date, []) if date else []:
            links[(*key, shipment_id)] = "unit_session"


def compute_links(
    session: Session, now: Optional[datetime.datetime] = None
) -> dict[LinkKey, str]:
    """Every link the stored data gives, as (request key, label id) ->
    basis. Reads only: on SQLite it takes no write lock.

    ``now`` (the current time by default) decides which unscanned boxes are
    still fresh.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
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
    return links


# The computed links, staged in a temporary table (private to the
# connection, so filling it takes no lock on the database) and copied from
# there into package_shipments.
_STAGED = sa.Table(
    "staged_package_shipments",
    sa.MetaData(),
    sa.Column("inmate_jurisdiction", sa.String(16), nullable=False),
    sa.Column("inmate_id", sa.Integer, nullable=False),
    sa.Column("request_index", sa.Integer, nullable=False),
    sa.Column("shipment_id", sa.Integer, nullable=False),
    sa.Column("basis", sa.String(20), nullable=False),
    prefixes=["TEMPORARY"],
)
# How many times the SQLite rebuild computes the links before giving up
# because the data changed under it every time.
REBUILD_ATTEMPTS = 5


class StaleLinks(RuntimeError):
    """The data changed during every rebuild attempt; nothing was written."""


def copy_staged_statement() -> sa.Insert:
    """The INSERT copying the staged links whose request and label still
    exist into ``package_shipments``.

    The jurisdiction is cast to the requests' own type (a native enum on
    PostgreSQL, which won't compare with or take a VARCHAR).
    """
    staged = _STAGED.c
    request = models.Request.__table__.c
    jurisdiction = sa.cast(staged.inmate_jurisdiction, models.Jurisdiction)
    return sa.insert(models.PackageShipment).from_select(
        ["inmate_jurisdiction", "inmate_id", "request_index", "shipment_id", "basis"],
        sa.select(
            jurisdiction,
            staged.inmate_id,
            staged.request_index,
            staged.shipment_id,
            staged.basis,
        )
        .join(
            models.Request.__table__,
            sa.and_(
                request.inmate_jurisdiction == jurisdiction,
                request.inmate_id == staged.inmate_id,
                request.index == staged.request_index,
            ),
        )
        .join(
            models.Shipment.__table__,
            models.Shipment.__table__.c.id == staged.shipment_id,
        ),
    )


def _copy_staged(session: Session) -> int:
    """Copy the staged links (``copy_staged_statement``); return how many."""
    copied = session.execute(copy_staged_statement())
    session.execute(sa.delete(_STAGED))
    return cast(sa.CursorResult, copied).rowcount


def stage_links(session: Session, links: dict[LinkKey, str]) -> None:
    """Put ``links`` in the connection's staging table, replacing any there."""
    _STAGED.create(session.connection(), checkfirst=True)
    session.execute(sa.delete(_STAGED))
    if links:
        session.execute(
            sa.insert(_STAGED),
            [
                {
                    "inmate_jurisdiction": jurisdiction,
                    "inmate_id": inmate_id,
                    "request_index": index,
                    "shipment_id": shipment_id,
                    "basis": basis,
                }
                for (jurisdiction, inmate_id, index, shipment_id), basis in sorted(
                    links.items()
                )
            ],
        )


def write_links(session: Session, links: dict[LinkKey, str]) -> int:
    """Replace every ``package_shipments`` row with ``links``; return how
    many were written.

    The links are staged first, then ``package_shipments`` is emptied and
    filled from the staged rows, so the write lock (SQLite's, from the
    delete to the caller's commit) is held only for those two statements.
    A link whose request or label was deleted after ``compute_links`` read
    it is dropped: the copy only takes links whose parents still exist,
    checked under that lock.
    """
    stage_links(session, links)
    session.execute(sa.delete(models.PackageShipment))
    return _copy_staged(session)


def data_version(session: Session) -> int:
    """SQLite's ``data_version`` on the session's connection: it changes
    when another connection commits, never for this one's own writes."""
    return int(session.execute(sa.text("PRAGMA data_version")).scalar_one())


def rebuild_links(session: Session, now: Optional[datetime.datetime] = None) -> int:
    """Rebuild every ``package_shipments`` row; return how many.

    Must start the session's transaction, and the caller's commit should
    follow at once. ``now`` is as for ``compute_links``.

    On SQLite the links are computed with no lock held (``compute_links``)
    and checked to come from one state of the database: ``data_version`` is
    read before the first read and again once the write lock is held (by
    emptying ``package_shipments``). If another connection committed in
    between, the attempt is rolled back and the links computed again, up to
    ``REBUILD_ATTEMPTS`` times; then ``StaleLinks`` is raised and the old
    links are kept. On any other database the reads and the write are one
    transaction at REPEATABLE READ, so they see one snapshot.
    """
    if session.get_bind().dialect.name != "sqlite":
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return write_links(session, compute_links(session, now))
    for _ in range(REBUILD_ATTEMPTS):
        before = data_version(session)
        stage_links(session, compute_links(session, now))
        session.execute(sa.delete(models.PackageShipment))
        if data_version(session) == before:
            return _copy_staged(session)
        session.rollback()
    raise StaleLinks(
        f"links not rebuilt: the data changed during each of {REBUILD_ATTEMPTS} "
        "attempts; the old links are kept"
    )
