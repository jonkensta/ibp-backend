"""FBOP inmate query implementation."""

import datetime
import json
import logging
import re
import typing

from .misc import run_curl_exec
from .types import QueryResult

LOGGER: logging.Logger = logging.getLogger(__name__)

URL = "https://www.bop.gov/PublicInfo/execute/inmateloc"

TEXAS_UNITS = {
    "BAS",
    "BML",
    "BMM",
    "BMP",
    "BSC",
    "BIG",
    "BRY",
    "CRW",
    "EDN",
    "FTW",
    "DAL",
    "HOU",
    "LAT",
    "REE",
    "RVS",
    "SEA",
    "TEX",
    "TRV",
}


def format_inmate_id(inmate_id: typing.Union[str, int]) -> str:
    """Format FBOP inmate IDs."""
    try:
        inmate_id = int(str(inmate_id).replace("-", ""))
    except ValueError as exc:
        raise ValueError("inmate ID must be a number") from exc

    inmate_id = f"{inmate_id:08d}"

    if len(inmate_id) != 8:
        raise ValueError("inmate ID must be less than 8 digits")

    return inmate_id[0:5] + "-" + inmate_id[5:8]


async def _curl_search_url(
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: typing.Optional[float] = None,
) -> dict:
    """Query FBOP using a curl subprocess call."""

    args = ["-G", "-f", "-s"]
    args.append(URL)

    params = {
        "age": "",
        "nameMiddle": "",
        "output": "json",
        "race": "",
        "sex": "",
        "todo": "query",
        "nameLast": last_name,
        "nameFirst": first_name,
        "inmateNum": inmate_id,
    }

    for key, value in params.items():
        args.append("--data-urlencode")
        args.append(f"{key}={value}")

    stdout = await run_curl_exec(args, timeout=timeout)
    return json.loads(stdout.decode("utf-8").strip())


async def query(
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: typing.Optional[float] = None,
) -> list[QueryResult]:
    """Private helper for querying FBOP."""

    if not inmate_id and not (first_name and last_name):
        return []

    data = await _curl_search_url(last_name, first_name, inmate_id, timeout)

    try:
        data = data["InmateLocator"]
    except KeyError:
        return []

    def data_to_inmate(entry):

        def parse_inmate_id(inmate_id: str) -> int:
            return int(re.sub(r"\D", "", inmate_id))

        inmate_id = parse_inmate_id(entry["inmateNum"])

        def parse_date(datestr):
            return datetime.datetime.strptime(datestr, "%m/%d/%Y").date()

        try:
            actual_release = parse_date(entry["actRelDate"])
        except ValueError:
            LOGGER.debug(
                "Failed to parse actual release date '%s'", entry["actRelDate"]
            )
            actual_release = None

        try:
            projected_release = parse_date(entry["projRelDate"])
        except ValueError:
            LOGGER.debug(
                "Failed to parse projected release date '%s'", entry["projRelDate"]
            )
            projected_release = None

        release = (
            actual_release
            or projected_release
            or entry["projRelDate"]
            or entry["actRelDate"]
            or None
        )

        if release is None:
            LOGGER.debug("Failed to retrieve any release date.")

        return QueryResult(
            id=inmate_id,
            jurisdiction="Federal",
            first_name=entry["nameFirst"],
            last_name=entry["nameLast"],
            unit=entry["faclCode"],
            race=entry.get("race", None),
            sex=entry.get("gender", None),
            url=None,
            release=release,
            datetime_fetched=datetime.datetime.now(),
        )

    inmates: typing.Iterable[QueryResult] = map(data_to_inmate, data)

    def is_in_texas(inmate: QueryResult) -> bool:
        return inmate.unit in TEXAS_UNITS

    inmates = filter(is_in_texas, inmates)

    def has_not_been_released(inmate: QueryResult) -> bool:
        if isinstance(inmate.release, datetime.date):
            released = datetime.date.today() >= inmate.release
        else:
            released = False

        return not released

    inmates = filter(has_not_been_released, inmates)

    return list(inmates)
