"""Sync EasyPost postage labels and their USPS tracking events into IBP.

Run as ``python -m ibp.shipment_sync`` (daily on ibp-server, see
``scripts/shipment-sync/README.md``). It only ever sends GET requests: it
never buys, refunds or changes anything at EasyPost.

A normal run lists the labels bought in the last ``WINDOW`` (35 days, so
late events are picked up), or since a day before the newest stored label if
that is older (so a long outage leaves no gap), in windows split at UTC
month starts, each paged
newest first with ``before_id``, and stores each label and every tracking
event not stored yet. Each stored label not listed in the run that is not
finished (``is_finished``) then has its tracker fetched on its own. Last, the
request links are rebuilt (``ibp.shipment_links``).

``--since YYYY-MM-DD`` lists every window back to that date instead (the
backfill, or a re-evaluation of every label's unit), then fetches by id the
stored labels bought since then that the listing missed; if it fails
part-way it exits 1 without rebuilding the links and must be re-run.
``--rebuild-links`` only rebuilds the links.

The key comes from ``--key-file``, the file named by ``EASYPOST_API_KEY_FILE``
or ``EASYPOST_API_KEY``, never from the server config, and must be a
production key (``EZAK...``). Running twice changes nothing the second time,
and a failed run is caught up by the next. Logs one summary line per run,
with counts only: no recipient names or addresses are logged or stored.
"""

import argparse
import dataclasses
import datetime
import decimal
import logging
import os
import re
import sys
import traceback
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import models
from .easypost_client import (  # pylint: disable=unused-import
    RETRIES,
    ApiError,
    EasyPost,
)
from .labels import RequestKey, parse_request_code
from .shipment_links import never_mailed, rebuild_links
from .unit_matching import UnitMatcher

logger = logging.getLogger(__name__)

# Labels bought this recently are listed on every run.
WINDOW = datetime.timedelta(days=35)
# After an outage longer than WINDOW, listing starts this long before the
# newest stored label.
RESUME_OVERLAP = datetime.timedelta(days=1)
# A label is finished (no longer polled) this long after its last delivery,
# or this long after purchase whatever happened.
DELIVERED_SETTLED = datetime.timedelta(days=60)
MAX_POLL_AGE = datetime.timedelta(days=90)
PAGE_SIZE = 100
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Tracker statuses that mean the label's trip is over and came back.
RETURNED = frozenset({"return_to_sender"})


class MissingKey(RuntimeError):
    """No usable EasyPost key was given."""


# --- the label reference tag -------------------------------------------------

TAG_PREFIX = "ibp1:"
_JURISDICTIONS = {"texas": "Texas", "federal": "Federal"}


@dataclasses.dataclass(frozen=True)
class Tag:
    """A parsed ``ibp1:`` reference tag.

    ``kind`` is ``request`` (``ibp1:req:<request code>``), ``unit``
    (``ibp1:unit:<jurisdiction>:<unit name>``) or ``manual``
    (``ibp1:manual``).
    """

    kind: str
    request: Optional[RequestKey] = None
    unit: Optional[tuple[str, str]] = None


def parse_reference(reference: Optional[str]) -> Optional[Tag]:
    """Parse a label's EasyPost ``reference``, or None if it is not a tag.

    Anything that is not exactly one of the three forms (no reference, as on
    every label bought before the shipping apps set one, or unknown text) is
    treated as absent.
    """
    if not reference:
        return None
    text = reference.strip()
    if text == f"{TAG_PREFIX}manual":
        return Tag("manual")
    if text.startswith(f"{TAG_PREFIX}req:"):
        key = parse_request_code(text[len(f"{TAG_PREFIX}req:") :])
        return Tag("request", request=key) if key is not None else None
    if text.startswith(f"{TAG_PREFIX}unit:"):
        jurisdiction, separator, name = text[len(f"{TAG_PREFIX}unit:") :].partition(":")
        jurisdiction = _JURISDICTIONS.get(jurisdiction.strip().lower(), "")
        if separator and jurisdiction and name.strip():
            return Tag("unit", unit=(jurisdiction, name.strip()))
    return None


# --- reading a label ---------------------------------------------------------

# The addressee line ends with the 8-digit inmate number, as
# ``addresses.format_addressee`` writes it: ``First Last #00012345``.
_INMATE_NUMBER = re.compile(r"#\s*(\d{8})\s*$")


def inmate_number(addressee: Optional[str]) -> Optional[str]:
    """Return the 8-digit inmate number ending an addressee line, if any."""
    match = _INMATE_NUMBER.search(addressee or "")
    return match[1] if match else None


def normalize(text: Optional[str]) -> str:
    """Uppercase, with every run of punctuation and spaces made one space."""
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()


class EventValues(NamedTuple):
    """One tracking event's stored values."""

    occurred_at: datetime.datetime
    local_time: Optional[str]
    status: Optional[str]
    status_detail: Optional[str]
    message: Optional[str]
    city: Optional[str]
    state: Optional[str]
    zip: Optional[str]
    occurrence: int

    def key(self) -> tuple:
        """The values that identify the event (as the unique index does)."""
        return event_key(self)


def event_key(event: Any) -> tuple:
    """Identify an event (an ``EventValues`` or a stored ``TrackingEvent``)."""
    return (
        event.occurred_at,
        event.status or "",
        event.status_detail or "",
        event.message or "",
        event.city or "",
        event.state or "",
        event.zip or "",
        event.occurrence,
    )


def parse_timestamp(value: str) -> datetime.datetime:
    """Parse an EasyPost timestamp into an aware UTC datetime."""
    moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def parse_events(tracker: Optional[dict[str, Any]]) -> list[EventValues]:
    """Every event in a tracker's ``tracking_details``, in the list's order.

    An event identical to an earlier one in the list gets the next
    ``occurrence``, so repeats are kept and a re-sync matches them up.
    ``signed_by`` and the rest of the tracker are not kept. An event without
    a time can't be placed and is skipped.
    """
    events = []
    seen: dict[tuple, int] = defaultdict(int)
    for detail in (tracker or {}).get("tracking_details") or []:
        if not detail.get("datetime"):
            continue
        location = detail.get("tracking_location") or {}
        values = EventValues(
            occurred_at=parse_timestamp(detail["datetime"]),
            local_time=detail.get("datetime_local") or None,
            status=detail.get("status") or None,
            status_detail=detail.get("status_detail") or None,
            message=detail.get("message") or None,
            city=location.get("city") or None,
            state=location.get("state") or None,
            zip=location.get("zip") or None,
            occurrence=0,
        )
        identity = values.key()[:-1]
        values = values._replace(occurrence=seen[identity])
        seen[identity] += 1
        events.append(values)
    return events


def cents(amount: Any) -> Optional[int]:
    """Dollars as EasyPost gives them (``"5.57"``) in whole cents."""
    if amount in (None, ""):
        return None
    try:
        return int(
            (decimal.Decimal(str(amount)) * 100).quantize(
                decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP
            )
        )
    except decimal.InvalidOperation:
        return None


@dataclasses.dataclass
class Label:  # pylint: disable=too-many-instance-attributes
    """A purchased label as stored: every ``shipments`` column but the sync
    time, and its events."""

    easypost_id: str
    tracking_code: str
    tracker_id: Optional[str]
    purchased_at: datetime.datetime
    weight_oz: Optional[float]
    cost_cents: Optional[int]
    service: Optional[str]
    reference: Optional[str]
    kind: str
    unit_jurisdiction: Optional[str]
    unit_name: Optional[str]
    request_jurisdiction: Optional[str]
    request_inmate_id: Optional[int]
    request_index: Optional[int]
    inmate_number: Optional[str]
    refund_status: Optional[str]
    tracker_status: Optional[str]
    events: list[EventValues]

    def columns(self) -> dict[str, Any]:
        """The ``shipments`` column values."""
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
            if field.name != "events"
        }


def read_label(
    shipment: dict[str, Any], units: UnitMatcher, unit_recipient: str
) -> Optional[Label]:
    """Read a listed shipment, or None if it has no label (never purchased).

    The kind comes from the reference tag; without one, a label whose
    addressee line ends in an inmate number is ``individual``, one addressed
    to ``unit_recipient`` (the shipping settings' mailroom line) is
    ``bulk``, and anything else is ``other``. The name and street address
    are read here and never stored, nor is a reference that is not a tag.
    """
    rate = shipment.get("selected_rate") or {}
    if not shipment.get("tracking_code") or not rate:
        return None
    to_address = shipment.get("to_address") or {}
    tracker = shipment.get("tracker") or {}
    label_info = shipment.get("postage_label") or {}
    tag = parse_reference(shipment.get("reference"))
    number = inmate_number(to_address.get("name"))

    unit = None
    if tag is not None and tag.unit is not None:
        unit = units.by_tag(*tag.unit)
    if unit is None:
        unit = units.by_address(to_address)

    if tag is not None:
        kind = {"request": "individual", "unit": "bulk"}.get(tag.kind, "other")
    elif number is not None:
        kind = "individual"
    elif unit_recipient and unit_recipient in normalize(
        f"{to_address.get('name') or ''} {to_address.get('company') or ''}"
    ):
        kind = "bulk"
    else:
        kind = "other"

    request = tag.request if tag is not None else None
    weight = (shipment.get("parcel") or {}).get("weight")
    return Label(
        easypost_id=shipment["id"],
        tracking_code=shipment["tracking_code"],
        tracker_id=tracker.get("id") or None,
        purchased_at=parse_timestamp(
            label_info.get("created_at") or shipment["created_at"]
        ),
        weight_oz=float(weight) if weight is not None else None,
        cost_cents=cents(rate.get("rate")),
        service=rate.get("service") or None,
        # Only our own tags: free text could be anything, a name included.
        reference=shipment.get("reference") if tag is not None else None,
        kind=kind,
        unit_jurisdiction=unit[0] if unit else None,
        unit_name=unit[1] if unit else None,
        request_jurisdiction=request.jurisdiction if request else None,
        request_inmate_id=request.inmate_id if request else None,
        request_index=request.index if request else None,
        inmate_number=number,
        refund_status=shipment.get("refund_status") or None,
        tracker_status=tracker.get("status") or None,
        events=parse_events(tracker),
    )


# --- when a label stops being polled -----------------------------------------


def is_finished(shipment: models.Shipment, now: datetime.datetime) -> bool:
    """Whether a stored label needs no more polling.

    Finished: 90 days after purchase; a label refunded (or refund submitted)
    and never scanned, which was never mailed; a label that went
    ``return_to_sender``; and 60 days after its last ``delivered`` event (a
    return after a delivery would have finished it already). The events must
    be loaded. Labels bought in the last ``WINDOW`` are listed every run
    anyway, so this only decides which older ones are fetched on their own.
    """
    if now - shipment.purchased_at >= MAX_POLL_AGE:
        return True
    if never_mailed(shipment):
        return True
    if shipment.tracker_status in RETURNED or any(
        event.status in RETURNED for event in shipment.events
    ):
        return True
    delivered = [e.occurred_at for e in shipment.events if e.status == "delivered"]
    return bool(delivered) and now - max(delivered) >= DELIVERED_SETTLED


# --- listing -----------------------------------------------------------------


def month_windows(
    start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Split ``[start, end)`` at UTC month starts, newest window first.

    The list endpoint spans about a month per query, so no window is longer.
    """
    windows = []
    high = end
    while high > start:
        low = high.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if low == high:
            low = (low - datetime.timedelta(days=1)).replace(day=1)
        low = max(low, start)
        windows.append((low, high))
        high = low
    return windows


def list_window(
    client: EasyPost, start: datetime.datetime, end: datetime.datetime
) -> Iterator[list[dict]]:
    """Yield each page of purchased shipments created in ``[start, end)``.

    The end is pushed a second later so a label bought on a boundary second
    is listed by one window or both (the caller keys on the shipment id).
    """
    params: dict[str, Any] = {
        "page_size": PAGE_SIZE,
        "purchased": "true",
        "start_datetime": start.strftime(TIMESTAMP_FORMAT),
        "end_datetime": (end + datetime.timedelta(seconds=1)).strftime(
            TIMESTAMP_FORMAT
        ),
    }
    while True:
        data = client.get("shipments", params)
        batch = data.get("shipments") or []
        if batch:
            yield batch
        if not batch or not data.get("has_more"):
            return
        params["before_id"] = batch[-1]["id"]


# --- storing -----------------------------------------------------------------


@dataclasses.dataclass
class Summary:  # pylint: disable=too-many-instance-attributes
    """Counts for the run's log line."""

    seen: int = 0
    new: int = 0
    updated: int = 0
    events_added: int = 0
    polled: int = 0
    poll_failures: int = 0
    links: int = 0
    fetched_by_id: int = 0

    def line(self) -> str:
        """The one-line summary (counts only)."""
        return (
            f"shipment sync: {self.seen} labels seen, {self.new} new, "
            f"{self.updated} updated, {self.fetched_by_id} fetched by id, "
            f"{self.events_added} events added, "
            f"{self.polled} trackers polled ({self.poll_failures} failed), "
            f"{self.links} links built"
        )


def add_events(shipment: models.Shipment, events: Iterable[EventValues]) -> int:
    """Add the events not already stored for ``shipment``; return how many."""
    stored = {event_key(event) for event in shipment.events}
    added = 0
    for values in events:
        if values.key() in stored:
            continue
        stored.add(values.key())
        shipment.events.append(models.TrackingEvent(**values._asdict()))
        added += 1
    return added


def store_label(
    session: Session, label: Label, now: datetime.datetime, summary: Summary
) -> None:
    """Insert or update a label and add its new events.

    ``synced_at`` is set only when the sync changes the row or adds an
    event, so an identical run writes nothing.
    """
    shipment = session.scalar(
        sa.select(models.Shipment)
        .where(models.Shipment.easypost_id == label.easypost_id)
        .options(selectinload(models.Shipment.events))
    )
    columns = label.columns()
    if shipment is None:
        shipment = models.Shipment(**columns, synced_at=now, events=[])
        session.add(shipment)
        summary.new += 1
        changed = True
    else:
        changed = False
        for name, value in columns.items():
            if getattr(shipment, name) != value:
                setattr(shipment, name, value)
                changed = True
        summary.updated += changed
    added = add_events(shipment, label.events)
    summary.events_added += added
    if added or changed:
        shipment.synced_at = now


def apply_tracker(
    shipment: models.Shipment,
    tracker: dict[str, Any],
    now: datetime.datetime,
    summary: Summary,
) -> None:
    """Update a stored label from its tracker, fetched on its own."""
    status = tracker.get("status") or None
    changed = status != shipment.tracker_status
    shipment.tracker_status = status
    added = add_events(shipment, parse_events(tracker))
    summary.updated += changed
    summary.events_added += added
    if added or changed:
        shipment.synced_at = now


def load_units(session: Session) -> UnitMatcher:
    """Match against the units table as it is now."""
    return UnitMatcher(session.scalars(sa.select(models.Unit)).all())


def load_unit_recipient(session: Session) -> str:
    """The mailroom recipient line bulk boxes are addressed to, normalized."""
    settings = session.get(models.ShippingSettings, models.SHIPPING_SETTINGS_ID)
    name = (
        settings.unit_recipient_name
        if settings is not None
        else models.DEFAULT_SHIPPING_SETTINGS["unit_recipient_name"]
    )
    return normalize(name)


def sync_listed(
    client: EasyPost,
    sessions: sessionmaker,
    start: datetime.datetime,
    now: datetime.datetime,
    summary: Summary,
) -> set[str]:
    """List and store every label bought since ``start``; return their ids.

    Each page is committed on its own, so a failed run keeps what it got.
    """
    seen: set[str] = set()
    with sessions() as session:
        units = load_units(session)
        unit_recipient = load_unit_recipient(session)
    for low, high in month_windows(start, now + datetime.timedelta(minutes=5)):
        for page in list_window(client, low, high):
            with sessions() as session, session.begin():
                for shipment in page:
                    if shipment["id"] in seen:
                        continue
                    label = read_label(shipment, units, unit_recipient)
                    if label is None:
                        continue
                    seen.add(shipment["id"])
                    summary.seen += 1
                    store_label(session, label, now, summary)
    return seen


def fetch_unlisted(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: EasyPost,
    sessions: sessionmaker,
    since: datetime.date,
    listed: set[str],
    now: datetime.datetime,
    summary: Summary,
) -> None:
    """Fetch by id, read and store each stored label bought since ``since``
    that the listing didn't return; add them to ``listed``.

    The listing goes by when the EasyPost shipment was created, so a label
    bought on or after ``since`` from a shipment created before it is not
    listed, but must be read again too for a ``--since`` re-evaluation.
    Each is committed on its own, like a page.
    """
    start = datetime.datetime.combine(since, datetime.time(), datetime.timezone.utc)
    with sessions() as session:
        stored = session.scalars(
            sa.select(models.Shipment.easypost_id)
            .where(models.Shipment.purchased_at >= start)
            .order_by(models.Shipment.purchased_at, models.Shipment.id)
        ).all()
        units = load_units(session)
        unit_recipient = load_unit_recipient(session)
    for easypost_id in stored:
        if easypost_id in listed:
            continue
        data = client.get(f"shipments/{urllib.parse.quote(easypost_id, safe='')}")
        summary.fetched_by_id += 1
        listed.add(easypost_id)
        label = read_label(data, units, unit_recipient)
        if label is not None and label.easypost_id == easypost_id:
            with sessions() as session, session.begin():
                store_label(session, label, now, summary)


def poll_unfinished(
    client: EasyPost,
    sessions: sessionmaker,
    listed: set[str],
    now: datetime.datetime,
    summary: Summary,
) -> None:
    """Fetch the tracker of each stored, unfinished label the run didn't list.

    A tracker EasyPost won't return is logged and counted (the run then
    fails) without stopping the others.
    """
    with sessions() as session:
        candidates = session.scalars(
            sa.select(models.Shipment)
            .where(models.Shipment.purchased_at >= now - MAX_POLL_AGE)
            .options(selectinload(models.Shipment.events))
            .order_by(models.Shipment.purchased_at)
        ).all()
        due = [
            shipment.id
            for shipment in candidates
            if shipment.easypost_id not in listed
            and shipment.tracker_id
            and not is_finished(shipment, now)
        ]
    for shipment_id in due:
        with sessions() as session, session.begin():
            shipment = session.scalar(
                sa.select(models.Shipment)
                .where(models.Shipment.id == shipment_id)
                .options(selectinload(models.Shipment.events))
            )
            if shipment is None or shipment.tracker_id is None:
                continue
            try:
                tracker = client.get(f"trackers/{shipment.tracker_id}")
            except ApiError as error:
                logger.warning(
                    "tracker for %s not fetched: %s", shipment.easypost_id, error
                )
                summary.poll_failures += 1
                continue
            summary.polled += 1
            apply_tracker(shipment, tracker, now, summary)


def relink(sessions: sessionmaker, now: Optional[datetime.datetime] = None) -> int:
    """Rebuild the request links in one transaction; return how many."""
    with sessions() as session, session.begin():
        return rebuild_links(session, now)


def listing_start(
    sessions: sessionmaker,
    now: datetime.datetime,
    since: Optional[datetime.date] = None,
) -> datetime.datetime:
    """When the run's listing starts: ``WINDOW`` ago, or ``since``, or a
    ``RESUME_OVERLAP`` before the newest stored label, whichever is earliest.

    With no label stored yet, the newest label plays no part (the first
    backfill is done with ``since``).
    """
    start = now - WINDOW
    if since is not None:
        start = min(
            start,
            datetime.datetime.combine(since, datetime.time(), datetime.timezone.utc),
        )
    with sessions() as session:
        newest = session.scalar(
            sa.select(models.Shipment.purchased_at)
            .order_by(models.Shipment.purchased_at.desc())
            .limit(1)
        )
    if newest is not None:
        start = min(start, newest - RESUME_OVERLAP)
    return start


def run(
    client: EasyPost,
    sessions: sessionmaker,
    now: Optional[datetime.datetime] = None,
    since: Optional[datetime.date] = None,
) -> Summary:
    """Sync labels from EasyPost, then rebuild the links; return the counts.

    ``since`` lists every label bought from that date (UTC) on, instead of
    the last ``WINDOW`` (see ``listing_start``), then fetches by id every
    stored label bought since then that the listing missed
    (``fetch_unlisted``): a re-evaluation of every label's unit. Anything
    that fails raises before the links are rebuilt.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    start = listing_start(sessions, now, since)
    summary = Summary()
    listed = sync_listed(client, sessions, start, now, summary)
    if since is not None:
        fetch_unlisted(client, sessions, since, listed, now, summary)
    poll_unfinished(client, sessions, listed, now, summary)
    summary.links = relink(sessions, now)
    return summary


# --- command line ------------------------------------------------------------


def read_key(key_file: Optional[str]) -> str:
    """Read the EasyPost key, failing with a clear message if there is none.

    From ``key_file``, else the file named by ``EASYPOST_API_KEY_FILE``,
    else ``EASYPOST_API_KEY``.
    """
    path = key_file or os.environ.get("EASYPOST_API_KEY_FILE")
    if path:
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise MissingKey(
                f"no EasyPost key: cannot read key file {path}: {error.strerror}"
            ) from error
        if not key:
            raise MissingKey(f"no EasyPost key: key file {path} is empty")
    else:
        key = os.environ.get("EASYPOST_API_KEY", "").strip()
        if not key:
            raise MissingKey(
                "no EasyPost key: pass --key-file, or set EASYPOST_API_KEY_FILE "
                "or EASYPOST_API_KEY"
            )
    if not key.startswith("EZAK"):
        raise MissingKey("refusing: not a production EasyPost key (EZAK...)")
    return key


def build_sessions() -> sessionmaker:
    """Synchronous sessions on the configured database."""
    # pylint: disable=import-outside-toplevel
    from .db_bootstrap import get_sync_database_url

    # Keep row values out of error messages, which reach Healthchecks.
    return sessionmaker(
        bind=sa.create_engine(get_sync_database_url(), hide_parameters=True)
    )


def crash_location(error: BaseException) -> str:
    """Where ``error`` was raised in IBP's code: the file (as
    ``ibp/shipment_sync.py``) and line of its traceback's innermost frame
    inside the ``ibp`` package."""
    package = Path(__file__).resolve().parent
    location = "unknown location"
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename).resolve()
        if path.is_relative_to(package):
            location = f"{path.relative_to(package.parent).as_posix()}:{line}"
    return location


def describe_failure(error: Exception) -> str:
    """A failure as safe to log: no SQL statement, parameters or row values.

    The run's output goes to Healthchecks. A database error is its class
    name, with SQLite's error code name for a DBAPI error that has one
    (``OperationalError: SQLITE_BUSY``), never its message, which can name
    a value. An ``ApiError`` is already safe (``EasyPost.get``). Anything
    else (a bug, or EasyPost data of an unexpected shape) is its class name
    and where IBP's code raised it.
    """
    if isinstance(error, sa.exc.DBAPIError):
        name = getattr(error.orig, "sqlite_errorname", None)
        if isinstance(name, str) and re.fullmatch(r"SQLITE_[A-Z_]{1,40}", name):
            return f"{type(error).__name__}: {name}"
        return type(error).__name__
    if isinstance(error, sa.exc.SQLAlchemyError):
        return type(error).__name__
    if isinstance(error, ApiError):
        return str(error)
    return f"{type(error).__name__} at {crash_location(error)}"


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m ibp.shipment_sync",
        description="Sync EasyPost labels and tracking events (GET only), "
        "then rebuild the request links.",
    )
    parser.add_argument(
        "--since",
        type=datetime.date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="list every label bought from this date on (one-time backfill)",
    )
    parser.add_argument(
        "--rebuild-links",
        action="store_true",
        help="only rebuild package_shipments; no EasyPost requests",
    )
    parser.add_argument(
        "--key-file", help="file holding the EasyPost production key (EZAK...)"
    )
    return parser.parse_args(argv)


def main(
    argv: Optional[list[str]] = None,
    sessions: Optional[sessionmaker] = None,
    client: Optional[EasyPost] = None,
) -> int:
    """Run the sync; return 0, 1 if anything failed, 2 without a key.

    Any failure but a missing key is logged as one line (``describe_failure``),
    never as a traceback. ``sessions`` and ``client`` are for tests.
    """
    args = parse_args(argv)
    try:
        if sessions is None:
            sessions = build_sessions()
        if args.rebuild_links:
            logger.info("shipment sync: %d links built", relink(sessions))
            return 0
        if client is None:
            try:
                client = EasyPost(read_key(args.key_file))
            except MissingKey as error:
                logger.error("%s", error)
                return 2
        summary = run(client, sessions, since=args.since)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("shipment sync failed: %s", describe_failure(error))
        if args.since is not None:
            # Pages already stored keep their new units; the links were not
            # rebuilt from that mix.
            logger.error(
                "shipment sync: re-evaluation since %s is incomplete and the "
                "links were not rebuilt; re-run with --since %s",
                args.since.isoformat(),
                args.since.isoformat(),
            )
        return 1
    logger.info("%s", summary.line())
    return 1 if summary.poll_failures else 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(main())
