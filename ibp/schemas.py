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
    """Pydantic model for inmate search results, including inmates and errors."""

    inmates: list[InmateSearchResult]
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
    ``ATTN: MAILROOM STAFF``) from the shipping settings.
    """

    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    jurisdiction: JurisdictionEnum
    id: int
    datetime_fetched: Optional[datetime.datetime] = None

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


class UpcBook(Book):
    """A book found by its UPC: ``isbn`` is the ISBN-13 the UPC maps to.

    ``upc_source`` says who mapped it: ``upcitemdb`` or ``volunteer``.
    """

    upc_source: str


class UpcMapping(BaseModel):
    """An ISBN a volunteer entered for a UPC the lookup didn't know."""

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


class PackageItemCreate(PackageItemBase):
    """Schema for recording a book in a package.

    ``source`` is ``isbn`` when the title and author came from an ISBN
    lookup (which needs an ISBN) or ``manual`` when typed in.
    """

    source: ItemSourceEnum

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
    """

    id: int
    source: ItemSourceEnum
    revision: int
    registered_at: Optional[datetime.datetime] = None
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


class PackageItemCreated(PackageItem):
    """A newly recorded package item.

    ``previously_sent`` lists the codes of this inmate's other requests that
    already contain the same ISBN (empty without an ISBN), so the station
    can warn about a repeat.
    """

    previously_sent: list[str] = []


class PackageInmate(InmateBase):
    """The inmate a package goes to, with their unit."""

    unit: Optional[Unit] = None

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
