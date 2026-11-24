"""Reading a listed EasyPost shipment into the values IBP stores.

The recipient's name and street address are read here to tell the label's
kind and destination, and never stored.
"""

import dataclasses
import datetime
import decimal
import math
import re
from collections import defaultdict
from typing import Any, NamedTuple, Optional

from .labels import RequestKey, parse_request_code
from .unit_matching import UnitMatcher

# --- checking EasyPost's values ----------------------------------------------

# EasyPost's ids and USPS tracking codes. Only values of these shapes are
# stored, so only they can reach a log line (e.g. a tracker warning).
SHIPMENT_ID = re.compile(r"shp_[0-9a-f]{8,64}")
TRACKER_ID = re.compile(r"trk_[0-9a-f]{8,64}")
TRACKING_CODE = re.compile(r"[A-Za-z0-9]{8,40}")


# EasyPost's tracker statuses.
TRACKER_STATUSES = frozenset(
    {
        "unknown",
        "pre_transit",
        "in_transit",
        "out_for_delivery",
        "delivered",
        "available_for_pickup",
        "return_to_sender",
        "failure",
        "cancelled",
        "error",
    }
)


class Malformed(ValueError):
    """A listed shipment or tracker isn't of the shape EasyPost documents.

    Its message is fixed text naming the field, never the value.
    """


def as_text(value: Any, field: str) -> Optional[str]:
    """A string field, or None for a missing or empty one."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise Malformed(f"{field} is not text")
    return value


def mapping(value: Any, field: str) -> dict[str, Any]:
    """An object field, ``{}`` for a missing one."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Malformed(f"{field} is not an object")
    return value


def tracker_status(value: Any) -> Optional[str]:
    """A tracker status, which must be one of EasyPost's when given."""
    status = as_text(value, "tracker status")
    if status is not None and status not in TRACKER_STATUSES:
        raise Malformed("tracker status is not one of EasyPost's")
    return status


def checked(value: Any, pattern: re.Pattern, field: str) -> Optional[str]:
    """A field that must match ``pattern`` when present."""
    found = as_text(value, field)
    if found is not None and not pattern.fullmatch(found):
        raise Malformed(f"{field} is not of EasyPost's shape")
    return found


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


def parse_timestamp(value: Any) -> datetime.datetime:
    """Parse an EasyPost timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        raise Malformed("timestamp is not text")
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Malformed("timestamp is not a time") from error
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
    details = mapping(tracker, "tracker").get("tracking_details") or []
    if not isinstance(details, list):
        raise Malformed("tracking_details is not a list")
    for detail in details:
        detail = mapping(detail, "tracking detail")
        if not detail.get("datetime"):
            continue
        location = mapping(detail.get("tracking_location"), "tracking_location")
        values = EventValues(
            occurred_at=parse_timestamp(detail["datetime"]),
            local_time=as_text(detail.get("datetime_local"), "datetime_local"),
            status=as_text(detail.get("status"), "status"),
            status_detail=as_text(detail.get("status_detail"), "status_detail"),
            message=as_text(detail.get("message"), "message"),
            city=as_text(location.get("city"), "city"),
            state=as_text(location.get("state"), "state"),
            zip=as_text(location.get("zip"), "zip"),
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


def parcel_weight(shipment: dict[str, Any]) -> Optional[float]:
    """The parcel's weight in ounces, if given."""
    weight = mapping(shipment.get("parcel"), "parcel").get("weight")
    if weight is None:
        return None
    try:
        if isinstance(weight, bool) or not isinstance(weight, (int, float, str)):
            raise ValueError
        ounces = float(weight)
        if not math.isfinite(ounces):
            raise ValueError
    except ValueError as error:
        raise Malformed("weight is not a number") from error
    return ounces


def recipient(shipment: dict[str, Any]) -> dict[str, Optional[str]]:
    """The ``to_address`` lines a label is read by (never stored)."""
    address = mapping(shipment.get("to_address"), "to_address")
    return {
        field: as_text(address.get(field), field)
        for field in ("name", "company", "street1", "street2", "zip")
    }


def label_kind(
    tag: Optional[Tag],
    number: Optional[str],
    to_address: dict[str, Optional[str]],
    unit_recipient: str,
) -> str:
    """``individual``, ``bulk`` or ``other`` (see ``read_label``)."""
    if tag is not None:
        return {"request": "individual", "unit": "bulk"}.get(tag.kind, "other")
    if number is not None:
        return "individual"
    if unit_recipient and unit_recipient in normalize(
        f"{to_address.get('name') or ''} {to_address.get('company') or ''}"
    ):
        return "bulk"
    return "other"


def destination(
    tag: Optional[Tag],
    number: Optional[str],
    to_address: dict[str, Optional[str]],
    units: UnitMatcher,
    unit_recipient: str,
) -> tuple[Optional[tuple[str, str]], str]:
    """The label's unit (from its tag, else its address) and its kind."""
    unit = None
    if tag is not None and tag.unit is not None:
        unit = units.by_tag(*tag.unit)
    conflict = False
    if unit is None:
        unit, conflict = units.locate(to_address)
    if conflict and tag is None:
        # Where it went is in doubt, so its addressee's number can't be
        # trusted to be an inmate at either place.
        return unit, "other"
    return unit, label_kind(tag, number, to_address, unit_recipient)


def read_label(
    shipment: dict[str, Any], units: UnitMatcher, unit_recipient: str
) -> Optional[Label]:
    """Read a listed shipment, or None if it has no label (never purchased).

    The kind comes from the reference tag; without one, a label whose
    address's evidence of its destination conflicts (``Destination``) is
    ``other``; else one whose addressee line ends in an inmate number is
    ``individual``, one addressed to ``unit_recipient`` (the shipping
    settings' mailroom line) is ``bulk``, and anything else is ``other``.
    The name and street address are read here and never stored, nor is a
    reference that is not a tag.

    Raises ``Malformed`` for a shipment not of EasyPost's shape: ids and the
    tracking code not of theirs (``SHIPMENT_ID``, ``TRACKER_ID``,
    ``TRACKING_CODE``), or a field of the wrong type.
    """
    shipment = mapping(shipment, "shipment")
    if checked(shipment.get("id"), SHIPMENT_ID, "id") is None:
        raise Malformed("id is missing")
    tracking_code = checked(
        shipment.get("tracking_code"), TRACKING_CODE, "tracking_code"
    )
    rate = mapping(shipment.get("selected_rate"), "selected_rate")
    if tracking_code is None or not rate:
        return None
    to_address = recipient(shipment)
    tracker = mapping(shipment.get("tracker"), "tracker")
    tracker_id = checked(tracker.get("id"), TRACKER_ID, "tracker id")
    label_info = mapping(shipment.get("postage_label"), "postage_label")
    reference = as_text(shipment.get("reference"), "reference")
    tag = parse_reference(reference)
    number = inmate_number(to_address.get("name"))

    unit, kind = destination(tag, number, to_address, units, unit_recipient)
    request = tag.request if tag is not None else None
    return Label(
        easypost_id=shipment["id"],
        tracking_code=tracking_code,
        tracker_id=tracker_id,
        purchased_at=parse_timestamp(
            label_info.get("created_at") or shipment.get("created_at")
        ),
        weight_oz=parcel_weight(shipment),
        cost_cents=cents(rate.get("rate")),
        service=as_text(rate.get("service"), "service"),
        # Only our own tags: free text could be anything, a name included.
        reference=reference if tag is not None else None,
        kind=kind,
        unit_jurisdiction=unit[0] if unit else None,
        unit_name=unit[1] if unit else None,
        request_jurisdiction=request.jurisdiction if request else None,
        request_inmate_id=request.inmate_id if request else None,
        request_index=request.index if request else None,
        inmate_number=number,
        refund_status=as_text(shipment.get("refund_status"), "refund_status"),
        tracker_status=tracker_status(tracker.get("status")),
        events=parse_events(tracker),
    )
