"""IBP sqlalchemy models."""

import datetime
from typing import Optional

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.types
from sqlalchemy import Date, DateTime, Enum, Integer, String, Text, TypeDecorator
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import mapped_column  # pylint: disable=no-name-in-module
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
)

from .base import config
from .db import Base


class TZDateTime(TypeDecorator):  # pylint: disable=too-many-ancestors
    """DateTime type that ensures timezone-awareness (UTC) when loading from database."""

    impl = DateTime
    cache_ok = True

    @property
    def python_type(self):
        """Return the Python type."""
        return datetime.datetime

    def __init__(self, *args, **kwargs):
        """Initialize with timezone=True for PostgreSQL TIMESTAMPTZ support."""
        kwargs["timezone"] = True
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value, dialect):
        """Process value before binding to database."""
        if value is not None and value.tzinfo is not None:
            # Convert to UTC before saving if it has timezone info
            value = value.astimezone(datetime.timezone.utc)
        return value

    def process_literal_param(self, value, dialect):
        """Process value for literal rendering."""
        return value

    def process_result_value(self, value, dialect):
        """Ensure datetime is timezone-aware when loaded from database."""
        if value is not None and value.tzinfo is None:
            # Assume UTC for timezone-naive datetimes
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value


# Disable common pylint false-positives for sqlalchemy ORM classes.
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-instance-attributes
# pylint: disable=unsubscriptable-object


class ReleaseDate(sqlalchemy.types.TypeDecorator):  # pylint: disable=too-many-ancestors
    """Inmate release date SQLAlchemy column type."""

    impl = sqlalchemy.types.String
    python_type = str

    cache_ok = True

    def process_bind_param(self, value, _):
        """Process value before binding to database."""
        if value is None:
            return None
        try:
            return value.isoformat()
        except AttributeError:
            return str(value)

    def process_literal_param(self, value, _):
        """Process value for literal inclusion in SQL string."""
        if value is None:
            return "NULL"
        try:
            return f"'{value.isoformat()}'"
        except AttributeError:
            return f"'{str(value)}'"

    def process_result_value(self, value, _):
        """Process value after fetching from database."""
        if value is None:
            return None
        try:
            return datetime.date.fromisoformat(value)
        except (ValueError, TypeError):
            return value


# pylint: disable=invalid-name
Jurisdiction = Enum("Texas", "Federal", name="jurisdiction_enum")


class HasInmateIndex:
    """Mixin for models associated with an Inmate."""

    inmate_jurisdiction: Mapped[str] = mapped_column(Jurisdiction, nullable=False)
    inmate_id: Mapped[int] = mapped_column(Integer, nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)

    @declared_attr
    def __table_args__(cls):  # pylint: disable=no-self-argument
        return (
            PrimaryKeyConstraint(
                "inmate_jurisdiction",
                "inmate_id",
                "index",
            ),
            ForeignKeyConstraint(
                ["inmate_jurisdiction", "inmate_id"],
                ["inmates.jurisdiction", "inmates.id"],
            ),
        )

    @declared_attr
    def inmate(cls) -> Mapped["Inmate"]:  # pylint: disable=no-self-argument
        """Declare the relationship to the Inmate model."""
        return relationship("Inmate", uselist=False, viewonly=True)


class Inmate(Base):
    """Inmate sqlalchemy model."""

    __tablename__ = "inmates"

    __table_args__ = (
        ForeignKeyConstraint(
            ["jurisdiction", "unit_name"],
            ["units.jurisdiction", "units.name"],
        ),
    )

    # Composite Primary Key: jurisdiction and id
    jurisdiction: Mapped[str] = mapped_column(
        Jurisdiction, primary_key=True, nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)

    first_name: Mapped[Optional[str]] = mapped_column(String)
    last_name: Mapped[Optional[str]] = mapped_column(String)

    unit_name: Mapped[Optional[str]] = mapped_column(String)
    unit: Mapped[Optional["Unit"]] = relationship(
        "Unit", foreign_keys=[jurisdiction, unit_name], uselist=False, viewonly=True
    )

    race: Mapped[Optional[str]] = mapped_column(String)
    sex: Mapped[Optional[str]] = mapped_column(String)
    release: Mapped[Optional[datetime.date]] = mapped_column(ReleaseDate)
    url: Mapped[Optional[str]] = mapped_column(String)

    datetime_fetched: Mapped[Optional[datetime.datetime]] = mapped_column(TZDateTime())

    # The index the inmate's next request gets. Only ever incremented (see
    # api.add_request), so a deleted request's index, and its printed label,
    # never point at a different request.
    next_request_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sqlalchemy.text("0")
    )

    requests: Mapped[list["Request"]] = relationship(
        "Request",
        back_populates="inmate",
        order_by="desc(Request.date_postmarked)",
        cascade="all, delete-orphan",
        collection_class=list,
    )

    comments: Mapped[list["Comment"]] = relationship(
        "Comment",
        back_populates="inmate",
        order_by="desc(Comment.datetime_created)",
        cascade="all, delete-orphan",
        collection_class=list,
    )

    lookups: Mapped[list["Lookup"]] = relationship(
        "Lookup",
        back_populates="inmate",
        order_by="desc(Lookup.datetime_created)",
        cascade="all, delete-orphan",
        collection_class=list,
    )

    def entry_is_fresh(self) -> bool:
        """Flag if an entry is fresh."""
        if self.datetime_fetched is None:
            return False

        age = datetime.datetime.now(datetime.timezone.utc) - self.datetime_fetched
        ttl_hours = config.getint("warnings", "inmates_cache_ttl")
        ttl = datetime.timedelta(hours=ttl_hours)
        return age < ttl


class Lookup(HasInmateIndex, Base):
    """Sqlalchemy for IBP lookups."""

    __tablename__ = "lookups"

    datetime_created: Mapped[datetime.datetime] = mapped_column(
        TZDateTime(), nullable=False
    )


class Request(HasInmateIndex, Base):
    """Sqlalchemy model for IBP requests."""

    __tablename__ = "requests"

    date_processed: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    date_postmarked: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    action: Mapped[str] = mapped_column(
        Enum("Filled", "Tossed", name="action_enum"), nullable=False
    )

    request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Incremented on every add, edit or delete of the request's items, in the
    # same transaction (see ibp/package_api.py).
    contents_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sqlalchemy.text("0")
    )

    items: Mapped[list["PackageItem"]] = relationship(
        "PackageItem",
        back_populates="request",
        order_by="PackageItem.id",
        cascade="all, delete-orphan",
        collection_class=list,
    )


# The columns of a request's key, as referenced by package items and
# registrations.
REQUEST_KEY_COLUMNS = ["inmate_jurisdiction", "inmate_id", "request_index"]
REQUEST_KEY_TARGETS = [
    "requests.inmate_jurisdiction",
    "requests.inmate_id",
    "requests.index",
]


class PackageItem(Base):
    """Sqlalchemy model for a book recorded in a filled request's package.

    ``id`` uses ``AUTOINCREMENT`` on SQLite so a deleted item's id is never
    given to a new item (a stale registration queue must not act on a
    different book). ``revision`` is incremented on every edit, and callers
    acting on an item pass the revision they saw. ``registered_at`` is set
    once the book is registered on the TDCJ Book Portal; a registered item is
    read-only.
    """

    __tablename__ = "package_items"

    __table_args__ = (
        ForeignKeyConstraint(REQUEST_KEY_COLUMNS, REQUEST_KEY_TARGETS),
        Index("ix_package_items_request", *REQUEST_KEY_COLUMNS),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    inmate_jurisdiction: Mapped[str] = mapped_column(Jurisdiction, nullable=False)
    inmate_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_index: Mapped[int] = mapped_column(Integer, nullable=False)

    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sqlalchemy.text("0")
    )

    isbn: Mapped[Optional[str]] = mapped_column(String(13))
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(
        Enum("isbn", "manual", name="item_source_enum", native_enum=False),
        nullable=False,
    )

    created_at: Mapped[datetime.datetime] = mapped_column(TZDateTime(), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(TZDateTime(), nullable=False)
    registered_at: Mapped[Optional[datetime.datetime]] = mapped_column(TZDateTime())

    request: Mapped["Request"] = relationship(
        "Request", back_populates="items", uselist=False
    )


class Registration(Base):
    """Sqlalchemy model for one successful TDCJ Book Portal registration.

    Append-only: the permanent record of what TDCJ got. ``item_id`` is kept
    (without a foreign key) after the item itself is deleted, and a request
    with registrations cannot be deleted. ``tdcj_number`` is the inmate
    number as sent to the portal (zero-padded to 8 digits).
    """

    __tablename__ = "registrations"

    __table_args__ = (
        ForeignKeyConstraint(REQUEST_KEY_COLUMNS, REQUEST_KEY_TARGETS),
        Index("ix_registrations_request", *REQUEST_KEY_COLUMNS),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    inmate_jurisdiction: Mapped[str] = mapped_column(Jurisdiction, nullable=False)
    inmate_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_index: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(100), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(100))
    tdcj_number: Mapped[str] = mapped_column(String(8), nullable=False)
    registered_at: Mapped[datetime.datetime] = mapped_column(
        TZDateTime(), nullable=False
    )


class Comment(HasInmateIndex, Base):
    """Sqlalchemy model for IBP comments."""

    __tablename__ = "comments"

    datetime_created: Mapped[datetime.datetime] = mapped_column(
        TZDateTime(), nullable=False
    )
    author: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)


class Unit(Base):
    """Sqlalchemy model for IBP units."""

    __tablename__ = "units"

    jurisdiction: Mapped[str] = mapped_column(Jurisdiction, primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)

    street1: Mapped[str] = mapped_column(String, nullable=False)
    street2: Mapped[Optional[str]] = mapped_column(String)

    city: Mapped[str] = mapped_column(String, nullable=False)
    zipcode: Mapped[str] = mapped_column(String(12), nullable=False)
    state: Mapped[str] = mapped_column(String(3), nullable=False)

    url: Mapped[Optional[str]] = mapped_column(String)

    shipping_method: Mapped[Optional[str]] = mapped_column(
        Enum("Box", "Individual", "Federal Tub", name="shipping_enum")
    )


# Production defaults for the shipping settings row, seeded by the
# ``b5e2d8c4a1f3`` migration and by ``ibp.db_bootstrap``, and returned by the
# API if the row is somehow missing.
SHIPPING_SETTINGS_ID = 1
DEFAULT_SHIPPING_SETTINGS: dict[str, Optional[str]] = {
    "return_name": "INSIDE BOOKS PROJECT",
    "return_street1": "PO BOX 301029",
    "return_street2": "12 ST BOOKS",
    "return_city": "AUSTIN",
    "return_state": "TX",
    "return_zipcode": "78703",
    "unit_recipient_name": "ATTN: MAILROOM STAFF",
}


class ShippingSettings(Base):
    """Sqlalchemy model for the organisation's shipping settings.

    A single row (``id == 1``) holding the return address printed on packages
    and the recipient line printed above unit mailroom addresses.
    """

    __tablename__ = "shipping_settings"

    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    return_name: Mapped[str] = mapped_column(String(100), nullable=False)
    return_street1: Mapped[str] = mapped_column(String(100), nullable=False)
    return_street2: Mapped[Optional[str]] = mapped_column(String(100))
    return_city: Mapped[str] = mapped_column(String(100), nullable=False)
    return_state: Mapped[str] = mapped_column(String(2), nullable=False)
    return_zipcode: Mapped[str] = mapped_column(String(10), nullable=False)

    unit_recipient_name: Mapped[str] = mapped_column(String(100), nullable=False)


class Book(Base):
    """Sqlalchemy model for the ISBN lookup cache.

    One row per ISBN-13: the title and author to use for that book, how they
    were decided (``source``: ``claude`` for a merge of the providers, a
    provider name when only it answered or Claude was unavailable, or
    ``manual``) and the raw provider candidates as JSON, kept so a bad merge
    can be audited later.
    """

    __tablename__ = "books"

    isbn: Mapped[str] = mapped_column(String(13), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[Optional[str]] = mapped_column(String(10))
    candidates: Mapped[Optional[str]] = mapped_column(Text)
    fetched_at: Mapped[datetime.datetime] = mapped_column(TZDateTime, nullable=False)


class Upc(Base):
    """Sqlalchemy model for the UPC -> ISBN lookup cache.

    Keyed by the full scanned code: the 12-digit UPC-A plus any 2- or 5-digit
    add-on, since on older mass-market paperbacks the UPC alone may identify
    only the publisher and price. ``isbn`` is NULL for a miss (no ISBN known),
    which is retried after a while. ``source`` is the service that answered
    (``upcitemdb``) or ``volunteer`` for an ISBN entered by hand after a miss.
    """

    __tablename__ = "upcs"

    upc: Mapped[str] = mapped_column(String(17), primary_key=True)
    isbn: Mapped[Optional[str]] = mapped_column(String(13))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    looked_up_at: Mapped[datetime.datetime] = mapped_column(TZDateTime, nullable=False)
