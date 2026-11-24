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
import logging
import os
import re
import sys
import traceback
import urllib.parse
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import models
from .easypost_client import (  # pylint: disable=unused-import
    RETRIES,
    ApiError,
    EasyPost,
)
from .label_reading import (  # pylint: disable=unused-import
    SHIPMENT_ID,
    EventValues,
    Label,
    Malformed,
    Tag,
    event_key,
    inmate_number,
    normalize,
    parse_events,
    parse_reference,
    as_text,
    read_label,
)
from .shipment_links import StaleLinks, never_mailed, rebuild_links
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
    A page that is not a list, or whose last shipment has no id of
    EasyPost's shape to page on, raises ``ApiError``.
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
        if not isinstance(batch, list):
            raise ApiError("GET shipments: page not of EasyPost's shape")
        if batch:
            yield batch
        if not batch or data.get("has_more") is not True:
            return
        last = batch[-1].get("id") if isinstance(batch[-1], dict) else None
        if not isinstance(last, str) or not SHIPMENT_ID.fullmatch(last):
            raise ApiError("GET shipments: page not of EasyPost's shape")
        params["before_id"] = last


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
    skipped: int = 0

    def line(self) -> str:
        """The one-line summary (counts only)."""
        return (
            f"shipment sync: {self.seen} labels seen, {self.new} new, "
            f"{self.updated} updated, {self.fetched_by_id} fetched by id, "
            f"{self.skipped} skipped, "
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
    """Update a stored label from its tracker, fetched on its own; raise
    ``Malformed``, changing nothing, for a tracker not of EasyPost's shape."""
    status = as_text(tracker.get("status"), "tracker status")
    events = parse_events(tracker)
    changed = status != shipment.tracker_status
    shipment.tracker_status = status
    added = add_events(shipment, events)
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
                    try:
                        label = read_label(shipment, units, unit_recipient)
                    except Malformed:
                        # Not stored, and its values are never logged.
                        summary.skipped += 1
                        continue
                    if label is None or label.easypost_id in seen:
                        continue
                    seen.add(label.easypost_id)
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


def loggable_id(shipment: models.Shipment) -> str:
    """The label's EasyPost id for a log line, if it is of EasyPost's shape
    (every id stored since ids were checked is)."""
    if SHIPMENT_ID.fullmatch(shipment.easypost_id or ""):
        return shipment.easypost_id
    return "a label with an id of another shape"


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
                apply_tracker(shipment, tracker, now, summary)
            except (ApiError, Malformed) as error:
                logger.warning(
                    "tracker for %s not fetched: %s", loggable_id(shipment), error
                )
                summary.poll_failures += 1
                continue
            summary.polled += 1


def relink(sessions: sessionmaker, now: Optional[datetime.datetime] = None) -> int:
    """Rebuild the request links and commit them; return how many.

    ``rebuild_links`` may roll back and retry, so it gets a session outside
    a ``begin()`` block; a failure closes the session, rolling back.
    """
    with sessions() as session:
        written = rebuild_links(session, now)
        session.commit()
        return written


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
    if isinstance(error, (ApiError, StaleLinks)):
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
