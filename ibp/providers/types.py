"""Types for IBP inmate providers."""

from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class QueryResult(BaseModel):
    """Base result of a provider query."""

    id: int
    jurisdiction: Literal["Federal", "Texas"]

    first_name: str
    last_name: str

    unit: str

    race: Optional[str] = None
    sex: Optional[str] = None

    url: Optional[str] = None
    release: Optional[str | datetime.date] = None

    datetime_fetched: datetime.datetime
