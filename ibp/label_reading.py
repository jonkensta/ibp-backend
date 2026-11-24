"""Reading a listed EasyPost shipment into the values IBP stores.

The recipient's name and street address are read here to tell the label's
kind and destination, and never stored.
"""

import dataclasses
import datetime
import decimal
import re
from collections import defaultdict
from typing import Any, NamedTuple, Optional

from .labels import RequestKey, parse_request_code
from .unit_matching import UnitMatcher

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
