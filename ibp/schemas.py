"""IBP pydantic schemas."""

import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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


class RequestBase(BaseModel):
    """Base schema for Request model."""

    date_processed: datetime.date
    date_postmarked: datetime.date
    action: ActionEnum


class CommentBase(BaseModel):
    """Base schema for Comment model."""

    datetime_created: datetime.datetime
    author: str
    body: str = Field(max_length=60)


class UnitCreate(UnitBase):
    """Schema for creating a new Unit."""


class RequestCreate(RequestBase):
    """Schema for creating a new Request."""


class CommentCreate(CommentBase):
    """Schema for creating a new Comment."""


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
    """Schema for Request records as stored in the database."""

    index: int

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


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

    unit: Unit

    requests: list[Request] = []
    comments: list[Comment] = []
    lookups: list[Lookup] = []

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic configuration for ORM mode."""

        from_attributes = True


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
