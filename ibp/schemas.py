"""IBP pydantic schemas."""

import datetime
import unicodedata
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .books import PORTAL_MAX_LENGTH, normalize_isbn
from .labels import label_package_id
from .upcs import normalize_code, valid_code


class JurisdictionEnum(str, Enum):
    """Enumeration for inmate jurisdictions."""

    TEXAS = "Texas"
    FEDERAL = "Federal"


class ActionEnum(str, Enum):
    """Enumeration for request actions."""

    FILLED = "Filled"
    TOSSED = "Tossed"


class ShippingMethodEnum(str, Enum):
    """Enumeration for unit shipping methods."""

    BOX = "Box"
    INDIVIDUAL = "Individual"
    FEDERAL_TUB = "Federal Tub"


class InmateStatusEnum(str, Enum):
    """Whether an inmate's provider confirmed them when a request was addressed.

    ``current``: the inmate's data was fresh, or the provider was re-queried
    and lists them (the address reflects any transfer). ``not_listed``: TDCJ
    answered without error but has no such inmate (it drops released
    inmates; Texas only, other providers' empty answers are ``unverified``).
    ``unverified``: the provider query failed, so the stored data is used as
    is.
    """

    CURRENT = "current"
    NOT_LISTED = "not_listed"
    UNVERIFIED = "unverified"


class UnitBase(BaseModel):
    """Base schema for Unit model."""

    name: str
    street1: str
    street2: Optional[str] = None
    city: str
    zipcode: str = Field(min_length=5, max_length=12)
    state: str = Field(min_length=2, max_length=3)
    url: Optional[str] = None
    jurisdiction: JurisdictionEnum
    shipping_method: Optional[ShippingMethodEnum] = None


class InmateBase(BaseModel):
    """Base schema for Inmate model."""

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    jurisdiction: JurisdictionEnum
    id: int
    race: Optional[str] = None
    sex: Optional[str] = None
    release: Optional[datetime.date | str] = None
    url: Optional[str] = None


class LookupBase(BaseModel):
    """Base schema for Lookup model."""

    datetime_created: datetime.datetime

    @field_serializer("datetime_created")
    def serialize_dt(self, dt: datetime.datetime, _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


class RequestBase(BaseModel):
    """Base schema for Request model."""

    date_processed: datetime.date
    date_postmarked: datetime.date
    action: ActionEnum
    request_id: Optional[int] = None


class CommentBase(BaseModel):
    """Base schema for Comment model."""

    datetime_created: datetime.datetime
    author: str
    body: str = Field(max_length=60)

    @field_serializer("datetime_created")
    def serialize_dt(self, dt: datetime.datetime, _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


class UnitCreate(UnitBase):
    """Schema for creating a new Unit."""

    name: str = Field(min_length=1)
    street1: str = Field(min_length=1)
    city: str = Field(min_length=1)


class RequestCreate(RequestBase):
    """Schema for creating a new Request."""


class CommentCreate(BaseModel):
    """Schema for creating a new Comment."""

    author: str
    body: str = Field(max_length=60)


class UnitUpdate(BaseModel):
    """Schema for updating an existing Unit."""

    name: Optional[str] = None
    street1: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    zipcode: Optional[str] = None
    state: Optional[str] = None
    url: Optional[str] = None
    jurisdiction: Optional[JurisdictionEnum] = None
    shipping_method: Optional[ShippingMethodEnum] = None


class RequestUpdate(BaseModel):
    """Schema for updating an existing Request."""

    date_processed: Optional[datetime.date] = None
    date_postmarked: Optional[datetime.date] = None
    action: Optional[ActionEnum] = None


class CommentUpdate(BaseModel):
    """Schema for updating an existing Comment."""

    author: Optional[str] = None
    body: Optional[str] = None


class Lookup(LookupBase):
    """Schema for Lookup records as stored in the database."""

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


class Request(RequestBase):
    """Schema for Request records as stored in the database.

    ``code`` is the request code printed on its label (e.g. ``TEX-00012345-0``),
    the request's address in ``/requests/{code}``.
    """

    index: int
    code: str
    contents_revision: int = 0

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def add_code(cls, data: Any) -> Any:
        """Compute ``code`` when validating a Request ORM object."""
        if isinstance(data, dict):
            return data
        names = [name for name in cls.model_fields.keys() if name != "code"]
        values = {name: getattr(data, name) for name in names}
        return {**values, "code": label_package_id(data)}


class Comment(CommentBase):
    """Schema for Comment records as stored in the database."""

    index: int

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


class Unit(UnitBase):
    """Schema for Unit records as stored in the database."""

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


class Inmate(InmateBase):
    """Schema for Inmate records as stored in the database."""

    datetime_fetched: Optional[datetime.datetime] = None

    unit: Optional[Unit] = None

    requests: list[Request] = []
    comments: list[Comment] = []
    lookups: list[Lookup] = []

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True

    @field_serializer("datetime_fetched")
    def serialize_dt(self, dt: Optional[datetime.datetime], _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


class InmateSearchResult(InmateBase):
    """Schema for inmate search result."""

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


class InmateSearchResults(BaseModel):
    """Pydantic model for inmate search results, including inmates and errors.

    ``stale_inmates`` holds the name-search matches left out of ``inmates``
    because their data is old and their provider, asked just now, would have
    returned them but did not (most likely released). See
    ``inmate_search.stale_inmate_clause``.
    """

    inmates: list[InmateSearchResult]
    stale_inmates: list[InmateSearchResult] = []
    errors: list[str]


class InmateWarnings(BaseModel):
    """Warnings about an inmate's data or status."""

    entry_age: Optional[str] = None
    release: Optional[str] = None


class RequestValidationWarnings(BaseModel):
    """Warnings when validating a new request before creation."""

    # Inmate warnings
    entry_age: Optional[str] = None
    release: Optional[str] = None

    # Request warnings
    postmarkdate: Optional[str] = None


class MonthlyMetricPoint(BaseModel):
    """Schema for a single month's metrics."""

    month: str  # Format: "YYYY-MM"
    filled_count: int
    tossed_count: int
    total_count: int


class RequestMetricsResponse(BaseModel):
    """Schema for request metrics response."""

    data: list[MonthlyMetricPoint]
    filters_applied: dict[str, Any]


class RequestAddress(BaseModel):
    """Read-only mailing address for shipping a request to an inmate.

    ``name`` is the pre-formatted addressee line (``First Last #00012345``);
    the remaining fields identify the request (``code`` is its label text,
    e.g. ``TEX-00012345-0``) and give the inmate's unit mailing address.
    ``unit_recipient_name`` is the configured mailroom recipient line (e.g.
    ``ATTN: MAILROOM STAFF``) from the shipping settings. ``inmate_status``
    says whether the inmate's provider confirmed them (see
    :class:`InmateStatusEnum`); ``datetime_fetched`` is when the stored data
    was last fetched from it.
    """

    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    jurisdiction: JurisdictionEnum
    id: int
    datetime_fetched: Optional[datetime.datetime] = None
    inmate_status: InmateStatusEnum

    request_index: int
    request_id: Optional[int] = None
    code: str
    action: ActionEnum

    unit_name: str
    unit_recipient_name: str
    street1: str
    street2: Optional[str] = None
    city: str
    state: str
    zipcode: str
    shipping_method: Optional[ShippingMethodEnum] = None

    @field_serializer("datetime_fetched")
    def serialize_dt(self, dt: Optional[datetime.datetime], _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


SETTINGS_MAX_LENGTH = 100

RequiredText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=SETTINGS_MAX_LENGTH
    ),
]


def blank_to_none(value: Optional[str]) -> Optional[str]:
    """Normalise a blank optional string to None."""
    return value or None


OptionalText = Annotated[
    Optional[
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=SETTINGS_MAX_LENGTH),
        ]
    ],
    AfterValidator(blank_to_none),
]

StateCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$"),
]

Zipcode = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^\d{5}(-\d{4})?$")
]


class ReturnAddress(BaseModel):
    """The organisation's return address printed on packages."""

    name: RequiredText
    street1: RequiredText
    street2: OptionalText = None
    city: RequiredText
    state: StateCode
    zipcode: Zipcode


class ShippingSettings(BaseModel):
    """Editable shipping settings.

    ``unit_recipient_name`` is the line printed above a unit's mailroom
    address (e.g. ``ATTN: MAILROOM STAFF``).
    """

    return_address: ReturnAddress
    unit_recipient_name: RequiredText


class ServerConfig(BaseModel):
    """Read-only settings from the config file: ``timezone`` is an IANA name."""

    timezone: str


class Book(BaseModel):
    """Title and author for an ISBN, from the lookup cache or the providers.

    ``source`` is ``claude`` when Claude merged the providers' answers (with
    its ``confidence``: ``high``, ``medium`` or ``low``), otherwise the
    provider used (``isbndb``, ``openlibrary``, ``google``, ``loc``) or
    ``manual``.
    """

    isbn: str
    title: str
    author: Optional[str] = None
    source: str
    confidence: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BookSuggestion(BaseModel):
    """A better spelling of a typed title and author (see ``ibp.book_search``).

    Offered only when Claude is sure (``confidence`` is ``high``) and the
    guard agrees; ``notes`` says why, briefly.
    """

    title: str
    author: str
    confidence: str
    notes: str


class BookSuggestions(BaseModel):
    """The answer to a suggestion request: ``suggestion`` is null for none."""

    suggestion: Optional[BookSuggestion] = None


class UpcBook(Book):
    """A book found by its UPC: ``isbn`` is the ISBN-13 the UPC maps to.

    ``upc_source`` says who mapped it: ``volunteer``.
    """

    upc_source: str


class UpcMapping(BaseModel):
    """An ISBN a volunteer scanned for a UPC the map didn't know."""

    isbn: str


class UpcNotFound(BaseModel):
    """Why a UPC lookup found no book.

    ``reason`` is ``no_isbn`` when no ISBN is known for the UPC (a volunteer
    may teach one), or ``no_book`` when the UPC maps to ``isbn`` but no
    provider knows that ISBN.
    """

    detail: str
    reason: Literal["no_isbn", "no_book"]
    isbn: Optional[str] = None


class ItemSourceEnum(str, Enum):
    """Where a package item's title and author came from."""

    ISBN = "isbn"
    MANUAL = "manual"


def clean_text(value: Any) -> Any:
    """Drop control and format characters and collapse whitespace.

    Whitespace controls (tabs, newlines) become spaces first, so words they
    separate stay separate. Non-strings are left for the type check.
    """
    if not isinstance(value, str):
        return value
    kept = (
        " " if char.isspace() else char
        for char in value
        if char.isspace() or unicodedata.category(char) not in ("Cc", "Cf")
    )
    return " ".join("".join(kept).split())


def isbn_or_none(value: Optional[str]) -> Optional[str]:
    """Normalize an ISBN to ISBN-13 (blank is None), or reject it."""
    if value is None or not value.strip():
        return None
    normalized = normalize_isbn(value)
    if normalized is None:
        raise PydanticCustomError("isbn", "Not a valid ISBN.")
    return normalized


# The TDCJ Book Portal requires a title and an author of PORTAL_MIN_LENGTH to
# PORTAL_MAX_LENGTH characters after trimming. Cleaning runs before the check.
PORTAL_MIN_LENGTH = 3


def portal_length(value: str) -> str:
    """Accept a cleaned title or author only if the portal would.

    The portal counts in JavaScript string length (UTF-16 code units), so a
    character outside the BMP, such as an emoji, counts as two.
    """
    units = len(value.encode("utf-16-le")) // 2
    if not PORTAL_MIN_LENGTH <= units <= PORTAL_MAX_LENGTH:
        raise PydanticCustomError(
            "portal_length",
            f"Must be {PORTAL_MIN_LENGTH} to {PORTAL_MAX_LENGTH} characters.",
        )
    return value


PortalText = Annotated[str, BeforeValidator(clean_text), AfterValidator(portal_length)]

# Largest value of a 64-bit signed integer column; bigger numbers reaching
# SQLite would overflow into a 500.
MAX_DB_INT = 2**63 - 1

Revision = Annotated[int, Field(ge=0, le=MAX_DB_INT)]

ItemIsbn = Annotated[Optional[str], AfterValidator(isbn_or_none)]


class PackageItemBase(BaseModel):
    """Base schema for a book recorded in a package.

    ``isbn`` is normalized to ISBN-13 (ISBN-10 converted, an 18-digit scan
    with a price add-on trimmed). Title and author have control characters
    removed and whitespace collapsed, and then must be 3 to 100 characters,
    as the portal requires. Both are required: a book without an author gets
    something like "Unknown".
    """

    isbn: ItemIsbn = None
    title: PortalText
    author: PortalText


def upc_or_none(value: Optional[str]) -> Optional[str]:
    """Normalize a UPC code (blank is None), or reject it."""
    if value is None or not value.strip():
        return None
    code = normalize_code(value.strip())
    if not valid_code(code):
        raise PydanticCustomError("upc", "Not a valid UPC.")
    return code


class PackageItemCreate(PackageItemBase):
    """Schema for recording a book in a package.

    ``source`` is ``isbn`` when the title and author came from an ISBN
    lookup (which needs an ISBN) or ``manual`` when typed in.
    ``remember_book`` saves the title and author for the ISBN's next scan,
    unless the lookup cache already has that ISBN. ``upc`` is the UPC code
    (with its add-on) scanned for the book: with an ISBN it's saved as that
    UPC's ISBN, unless the UPC is already mapped. A bare 12-digit UPC is
    never saved (many titles share it).
    """

    source: ItemSourceEnum
    remember_book: bool = False
    upc: Annotated[Optional[str], AfterValidator(upc_or_none)] = None

    @model_validator(mode="after")
    def check_source(self) -> "PackageItemCreate":
        """An item from an ISBN lookup must have its ISBN."""
        if self.source == ItemSourceEnum.ISBN and self.isbn is None:
            raise ValueError("An item from an ISBN lookup needs its ISBN.")
        return self


class PackageItemUpdate(PackageItemBase):
    """Schema for editing a package item (replaces title, author and ISBN).

    ``revision`` is the item revision the edit is based on (409 if stale).
    ``update_cache`` also saves the title and author to the ISBN lookup
    cache, so future scans of that ISBN get them; without it the cache is
    untouched.
    """

    revision: Revision
    update_cache: bool = False


class PackageItem(PackageItemBase):
    """Schema for a package item as stored.

    ``registered_at`` is empty until the book is registered on the TDCJ
    Book Portal (Texas only); a registered item is read-only.
    ``confirmation_number`` is the portal's, when the extension reported it.
    """

    id: int
    source: ItemSourceEnum
    revision: int
    registered_at: Optional[datetime.datetime] = None
    confirmation_number: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("registered_at", "created_at", "updated_at")
    def serialize_dt(self, dt: Optional[datetime.datetime], _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


Remembered = Literal["isbn", "upc"]


class PackageItemCreated(PackageItem):
    """A newly recorded package item.

    ``previously_sent`` lists the codes of this inmate's other requests that
    already contain the same ISBN (empty without an ISBN), so the station
    can warn about a repeat. ``remembered`` says what this add saved for
    the next copy: ``isbn`` (title and author for the ISBN) and ``upc`` (the
    UPC's ISBN).
    """

    previously_sent: list[str] = []
    remembered: list[Remembered] = []


class PackageInmate(InmateBase):
    """The inmate a package goes to, with their unit.

    ``pronouns`` are the ones the person stated in a letter, if any, shown at
    the packing station (without who recorded them or when).
    """

    unit: Optional[Unit] = None
    pronouns: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class Package(BaseModel):
    """A filled request with its inmate, books and the inmate's warnings."""

    request: Request
    inmate: PackageInmate
    items: list[PackageItem]
    warnings: InmateWarnings


class RegistrationCreate(BaseModel):
    """Report a book as registered on the portal.

    ``revision`` is the item revision the portal form was filled from.
    """

    revision: Revision


ConfirmationNumber = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9-]+$"
    ),
]

TdcjNumber = Annotated[str, StringConstraints(pattern=r"^\d{8}$")]


class PortalRegistrationCreate(PackageItemBase):
    """A book the portal just saved, reported from the extension's scan mode.

    ``title`` and ``author`` are what was submitted to the portal and
    ``tdcj_number`` the inmate it went to (8 digits, which must be the
    request's inmate). ``confirmation_number`` is the one the portal showed
    on its success page; reporting the same one again for the same request
    is a harmless retry.
    """

    tdcj_number: TdcjNumber
    confirmation_number: ConfirmationNumber


class RegistrationQueueEntry(BaseModel):
    """A Texas book not registered on the portal yet.

    ``tdcj_number`` is the inmate number zero-padded to 8 digits, as the
    portal shows it.
    """

    code: str
    item_id: int
    revision: int
    title: str
    author: Optional[str] = None
    tdcj_number: str


# Pronouns an inmate stated in a letter (see the README). Kept tight so the
# field cannot become a place for notes.
PRONOUNS_MAX_LENGTH = 30
PRONOUNS_PATTERN = r"^[A-Za-z][A-Za-z /,.'-]*$"
SET_BY_MAX_LENGTH = 40

StatedPronouns = Annotated[
    str,
    BeforeValidator(clean_text),
    StringConstraints(
        min_length=1, max_length=PRONOUNS_MAX_LENGTH, pattern=PRONOUNS_PATTERN
    ),
]

VolunteerInitials = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=SET_BY_MAX_LENGTH
    ),
]


class PronounsSet(BaseModel):
    """Record the pronouns an inmate stated in a letter.

    ``pronouns`` is cleaned like other free text and must then be 1 to 30
    letters, spaces and ``/,.'-``, starting with a letter. ``set_by`` is the
    recording volunteer's initials.
    """

    pronouns: StatedPronouns
    set_by: VolunteerInitials


class PronounsClear(BaseModel):
    """Clear an inmate's recorded pronouns; ``set_by`` is the volunteer's initials."""

    set_by: VolunteerInitials


class InmatePronouns(BaseModel):
    """An inmate's recorded pronouns, who recorded them and when."""

    pronouns: str
    set_by: str
    set_at: datetime.datetime

    @field_serializer("set_at")
    def serialize_dt(self, dt: datetime.datetime, _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


class InmateView(Inmate):
    """An inmate as the inmate view shows it, with any recorded pronouns.

    Only ``GET /inmates/{jurisdiction}/{inmate_id}`` and its lookups ``POST``
    return this. All three pronouns fields are empty unless the person
    stated their pronouns in a letter; they are never inferred from ``sex``.
    Other inmate responses (search results, the legacy inmate routes, request
    addresses) leave them out.
    """

    pronouns: Optional[str] = None
    pronouns_set_by: Optional[str] = None
    pronouns_set_at: Optional[datetime.datetime] = None

    @field_serializer("pronouns_set_at")
    def serialize_pronouns_set_at(self, dt: Optional[datetime.datetime], _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
        return dt.isoformat()


def _utc_isoformat(dt: Optional[datetime.datetime]) -> Optional[str]:
    """Serialize a datetime as ISO 8601, taking a naive one as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


class ShipmentKindEnum(str, Enum):
    """What a postage label was for, as the shipment sync read it."""

    INDIVIDUAL = "individual"
    BULK = "bulk"
    OTHER = "other"


class LinkBasisEnum(str, Enum):
    """Which rule linked a request to a label (see ibp/shipment_links.py)."""

    REFERENCE = "reference"
    INMATE_NUMBER = "inmate_number"
    UNIT_SESSION = "unit_session"


class TrackingEvent(BaseModel):
    """One USPS tracking event of a label, as EasyPost reported it.

    ``occurred_at`` is UTC; ``local_time`` is the carrier's local time
    string, when given.
    """

    occurred_at: datetime.datetime
    local_time: Optional[str] = None
    status: Optional[str] = None
    status_detail: Optional[str] = None
    message: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("occurred_at")
    def serialize_dt(self, dt: datetime.datetime, _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        return _utc_isoformat(dt)


class RequestShipment(BaseModel):
    """A label a request is linked to, with its tracking events (oldest first).

    ``basis`` says why it is linked: ``reference`` (the label was tagged
    with the request), ``inmate_number`` (an untagged label to the inmate)
    or ``unit_session`` (a bulk box to the inmate's unit).
    """

    tracking_code: str
    purchased_at: datetime.datetime
    kind: ShipmentKindEnum
    basis: LinkBasisEnum
    tracker_status: Optional[str] = None
    events: list[TrackingEvent]

    @field_serializer("purchased_at")
    def serialize_dt(self, dt: datetime.datetime, _info):
        """Serialize datetime to ISO format with UTC timezone if naive."""
        return _utc_isoformat(dt)


class DeliveryMethodEnum(str, Enum):
    """Which links the delivery latency metric counts: every link, a
    request's own label (``reference``, ``inmate_number``) or a bulk box
    (``unit_session``)."""

    ALL = "all"
    INDIVIDUAL = "individual"
    BULK = "bulk"


class LatencyQuartiles(BaseModel):
    """Days from postmark to delivery for one bound, over a month's delivered
    requests: the 25th percentile, median and 75th percentile, and the
    median transit (label purchase to delivery) for that bound."""

    p25: float
    median: float
    p75: float
    median_transit: float


class DeliveryLatencyMonth(BaseModel):
    """Postmark-to-delivery latency of the Filled requests postmarked in one
    month (``YYYY-MM``); see ``ibp/delivery_latency.py``.

    ``filled`` counts every Filled request; ``linked`` those with a link
    the method counts; ``delivered`` those with a delivered label that are
    in the statistics; ``returned`` those whose every label came back;
    ``excluded`` those delivered with a latency under 0 or over 365 days.
    The rest of ``linked`` are not delivered yet. The latency and stage
    fields are null when ``delivered`` is 0.
    """

    month: str
    filled: int
    linked: int
    delivered: int
    returned: int
    excluded: int
    earliest: Optional[LatencyQuartiles] = None
    latest: Optional[LatencyQuartiles] = None
    median_handling: Optional[float] = None
    median_waiting: Optional[float] = None


class DeliveryLatencyResponse(BaseModel):
    """Schema for the delivery latency metric response."""

    data: list[DeliveryLatencyMonth]
    filters_applied: dict[str, Any]
