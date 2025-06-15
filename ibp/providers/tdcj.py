"""TDCJ inmate query implementation."""

import datetime
import logging
import re
import typing
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from nameparser import HumanName  # type: ignore[import]

from .misc import run_curl_exec
from .types import QueryResult

LOGGER: logging.Logger = logging.getLogger(__name__)

BASE_URL = "https://inmate.tdcj.texas.gov"
SEARCH_PATH = "InmateSearch/search.action"
SEARCH_URL = urljoin(BASE_URL, SEARCH_PATH)


def format_inmate_id(inmate_id: typing.Union[int, str]) -> str:
    """Format a TDCJ inmate ID."""
    inmate_id = int(inmate_id)
    return f"{inmate_id:08d}"


async def _curl_search_url(
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: float | None = None,
):
    args = [
        "--ipv4",
        "-d",
        "btnSearch=Search",
        "-d",
        "gender=ALL",
        "-d",
        "race=ALL",
        "-d",
        f"tdcj={inmate_id}",
        "-d",
        f"lastName={last_name}",
        "-d",
        f"firstName={first_name}",
        "-d",
        "page=index",
        "-d",
        "sid=",
        SEARCH_URL,
    ]

    return await run_curl_exec(args, timeout=timeout)


async def query(  # pylint: disable=too-many-locals
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: float | None = None,
) -> list[QueryResult]:
    """Private helper for querying TDCJ."""

    html = await _curl_search_url(last_name, first_name, inmate_id, timeout)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "tdcj_table"})

    if table is None or not isinstance(table, Tag):
        return []

    for linebreak in table.find_all("br"):
        linebreak.replace_with(" ")

    rows = iter(table.find_all("tr"))

    try:
        next(rows)  # First row contains nothing.
        header = next(rows)
    except StopIteration:
        return []

    # Second row contains the column names.
    keys = [ele.text.strip() for ele in header.find_all("th")]

    def row_to_entry(row):
        values = [ele.get_text().strip() for ele in row.find_all("td")]
        if not values:
            return None
        entry = dict(zip(keys, values))
        anchor = row.find("a")
        entry["href"] = anchor.get("href") if anchor is not None else None
        return entry

    entries: typing.Iterable[dict[str, str]] = filter(None, map(row_to_entry, rows))

    def entry_to_inmate(entry: dict):
        """Convert TDCJ inmate entry to inmate dictionary."""

        def parse_inmate_id(inmate_id: str) -> int:
            return int(re.sub(r"\D", "", inmate_id))

        inmate_id = parse_inmate_id(entry["TDCJ Number"])

        name = HumanName(entry.get("Name", ""))
        first: str = name.first
        last: str = name.last

        def build_url(href):
            return urljoin(BASE_URL, href)

        url = build_url(str(entry["href"])) if "href" in entry else None

        def parse_release_date(release):
            return datetime.datetime.strptime(release, "%Y-%m-%d").date()

        release = entry["Projected Release Date"]

        try:
            release = parse_release_date(release)
        except ValueError:
            LOGGER.debug("Failed to parse release date '%s'", release)

        return QueryResult(
            id=inmate_id,
            jurisdiction="Texas",
            first_name=first,
            last_name=last,
            unit=entry["Unit of Assignment"],
            race=entry.get("Race", None),
            sex=entry.get("Gender", None),
            url=url,
            release=release,
            datetime_fetched=datetime.datetime.now(),
        )

    return list(map(entry_to_inmate, entries))
