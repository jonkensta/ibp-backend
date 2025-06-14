"""Types for IBP inmate providers."""

import datetime
import typing


class QueryResult(typing.TypedDict):
    """Base result of a query."""

    id: int
    jurisdiction: typing.Literal["Federal"] | typing.Literal["Texas"]

    first_name: str
    last_name: str

    unit: str

    race: typing.Optional[str]
    sex: typing.Optional[str]

    url: typing.Literal[None]
    release: typing.Optional[str | datetime.date]

    datetime_fetched: datetime.datetime
