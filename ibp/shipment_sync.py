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
one-time backfill); ``--rebuild-links`` only rebuilds the links.

The key comes from ``--key-file``, the file named by ``EASYPOST_API_KEY_FILE``
or ``EASYPOST_API_KEY``, never from the server config, and must be a
production key (``EZAK...``). Running twice changes nothing the second time,
and a failed run is caught up by the next. Logs one summary line per run,
with counts only: no recipient names or addresses are logged or stored.
"""

import argparse
import base64
import dataclasses
import datetime
import decimal
import http.client
import json
import logging
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import models
from .labels import RequestKey, parse_request_code
from .shipment_links import never_mailed, rebuild_links
from .unit_places import Places, display_name, leading_number, squeeze, zip5

logger = logging.getLogger(__name__)

API = "https://api.easypost.com/v2"
# Labels bought this recently are listed on every run.
WINDOW = datetime.timedelta(days=35)
# After an outage longer than WINDOW, listing starts this long before the
# newest stored label.
RESUME_OVERLAP = datetime.timedelta(days=1)
# A label is finished (no longer polled) this long after its last delivery,
# or this long after purchase whatever happened.
DELIVERED_SETTLED = datetime.timedelta(days=60)
MAX_POLL_AGE = datetime.timedelta(days=90)
# Seconds between EasyPost requests, and the retry policy for 429 and 5xx.
PACE_SECONDS = 0.3
RETRIES = 6
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PAGE_SIZE = 100
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Tracker statuses that mean the label's trip is over and came back.
RETURNED = frozenset({"return_to_sender"})


class ApiError(RuntimeError):
    """An EasyPost request failed."""


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


class UnitMatcher:
    """Finds the IBP unit a label went to, by its tag, name or address."""

    def __init__(self, units: Iterable[models.Unit]):
        units = list(units)
        self._places = Places(units)
        self._by_tag: dict[tuple[str, str], tuple[str, str]] = {}
        self._by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self._by_number: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for unit in units:
            key = (unit.jurisdiction, unit.name)
            self._by_tag[(unit.jurisdiction, unit.name.upper())] = key
            for name in {squeeze(unit.name), squeeze(display_name(unit))} - {""}:
                self._by_name[name].append((*key, zip5(unit.zipcode)))
            for street in (unit.street1, unit.street2):
                number = leading_number(street)
                if number is not None:
                    self._by_number[(number, zip5(unit.zipcode))].add(key)

    def by_tag(self, jurisdiction: str, name: str) -> Optional[tuple[str, str]]:
        """The unit a tag names, if IBP has it (unit names match in any case)."""
        return self._by_tag.get((jurisdiction, name.upper()))

    def by_address(self, address: dict[str, Any]) -> Optional[tuple[str, str]]:
        """The one place a label's address names or is at, or None.

        Units store the street in either line (usually the unit's display
        name in ``street1`` and the street in ``street2``), and EasyPost's
        verified addresses come back in USPS spelling with the name moved to
        ``street2``, so whole lines are not compared. First, a ``street1``,
        ``street2`` or ``company`` line that is a unit's key name or display
        name (``unit_places.display_name``; letters and digits only, any
        case) names that unit, whatever the ZIP; a name units in more than
        one place have (Texas and Federal, or one display name in two towns)
        counts only if the ZIP leaves one place. Otherwise the house number
        either street line starts with, with the 5-digit ZIP, must lead the
        ``street1`` or ``street2`` of units in one place. Units in different
        places at one address are told apart only by name; anything
        ambiguous is unknown. The unit returned is its place's
        representative (``unit_places.Places``), so units that are one
        place give one answer.
        """
        zipcode = zip5(address.get("zip"))
        named = {
            unit
            for field in ("street1", "street2", "company")
            for unit in self._by_name.get(squeeze(address.get(field)), [])
        }
        if named:
            place = self._places.one_place((j, name) for j, name, _ in named)
            if place is None:
                place = self._places.one_place(
                    (j, name) for j, name, unit_zip in named if unit_zip == zipcode
                )
            return place
        at_number: set[tuple[str, str]] = set()
        for field in ("street1", "street2"):
            number = leading_number(address.get(field))
            if number is not None:
                at_number |= self._by_number.get((number, zipcode), set())
        return self._places.one_place(at_number)


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


# --- EasyPost ----------------------------------------------------------------

Opener = Callable[..., Any]


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Fails every redirect, as an ``HTTPError`` with its 3xx status:
    following one would send the key's ``Authorization`` header on."""

    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def build_opener() -> Opener:
    """``urlopen`` for EasyPost: the standard one, but never redirected."""
    return urllib.request.build_opener(RefuseRedirects).open


class EasyPost:  # pylint: disable=too-few-public-methods
    """A GET-only EasyPost client, paced, retrying 429, 5xx, network errors
    and unreadable responses with backoff; a request that still fails raises
    ``ApiError``.

    A redirect is not followed: it fails as ``HTTP 3xx``. ``urlopen``
    (``build_opener()`` by default) and ``sleep`` are for tests.
    """

    def __init__(
        self,
        key: str,
        urlopen: Optional[Opener] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._auth = base64.b64encode(f"{key}:".encode()).decode()
        self._urlopen = urlopen if urlopen is not None else build_opener()
        self._sleep = sleep
        self.requests = 0

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        """GET ``path`` and return its JSON, retrying transient failures."""
        url = f"{API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Basic {self._auth}",
                "User-Agent": "ibp-shipment-sync",
            },
        )
        for attempt in range(RETRIES):
            if self.requests:
                self._sleep(PACE_SECONDS)
            self.requests += 1
            try:
                with self._urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in RETRY_STATUSES or attempt == RETRIES - 1:
                    code = error_code(error)
                    raise ApiError(
                        f"GET {path}: HTTP {error.code}" + (f" {code}" if code else "")
                    ) from error
                wait = retry_after(error) or 2.0**attempt
                logger.warning(
                    "GET %s: HTTP %d, retrying in %ss", path, error.code, wait
                )
                self._sleep(wait)
            except urllib.error.URLError as error:
                if attempt == RETRIES - 1:
                    raise ApiError(f"GET {path}: {error.reason}") from error
                logger.warning("GET %s: %s, retrying", path, error.reason)
                self._sleep(2.0**attempt)
            except (OSError, http.client.HTTPException, ValueError) as error:
                # A read timeout, a dropped or truncated body, or bad JSON.
                failure = type(error).__name__
                if attempt == RETRIES - 1:
                    raise ApiError(f"GET {path}: {failure} reading response") from error
                logger.warning("GET %s: %s reading response, retrying", path, failure)
                self._sleep(2.0**attempt)
        raise ApiError(f"GET {path}: giving up after retries")


_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def error_code(error: urllib.error.HTTPError) -> Optional[str]:
    """EasyPost's error ``code`` (e.g. ``NOT_FOUND``) from an error
    response's JSON body, if it has one.

    Nothing else of the body is kept: its message and field errors can quote
    an address, and the run's output goes to Healthchecks.
    """
    try:
        body = json.loads(error.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None
    detail = body.get("error") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


def retry_after(error: urllib.error.HTTPError) -> Optional[float]:
    """The seconds a ``Retry-After`` header asks for (capped at 60), if any."""
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None


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
class Summary:
    """Counts for the run's log line."""

    seen: int = 0
    new: int = 0
    updated: int = 0
    events_added: int = 0
    polled: int = 0
    poll_failures: int = 0
    links: int = 0

    def line(self) -> str:
        """The one-line summary (counts only)."""
        return (
            f"shipment sync: {self.seen} labels seen, {self.new} new, "
            f"{self.updated} updated, {self.events_added} events added, "
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
    the last ``WINDOW`` (see ``listing_start``).
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    start = listing_start(sessions, now, since)
    summary = Summary()
    listed = sync_listed(client, sessions, start, now, summary)
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
    name, with the driver's first message line for a DBAPI error (``database
    is locked``); an ``ApiError`` is already safe. Anything else (a bug, or
    EasyPost data of an unexpected shape) is its class name and where IBP's
    code raised it: its message could hold a value from a label.
    """
    if isinstance(error, sa.exc.DBAPIError):
        lines = str(error.orig).splitlines()
        message = lines[0][:200] if lines else ""
        return f"{type(error).__name__}: {message}" if message else type(error).__name__
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
        return 1
    logger.info("%s", summary.line())
    return 1 if summary.poll_failures else 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(main())
