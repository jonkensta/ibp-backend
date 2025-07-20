"""TDCJ inmate query implementation."""

import datetime
import logging
import re
import typing
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from nameparser import HumanName  # type: ignore[import]

from .errors import ProviderError
from .misc import run_curl_exec
from .types import QueryResult

LOGGER: logging.Logger = logging.getLogger(__name__)

BASE_URL = "https://inmate.tdcj.texas.gov"
SEARCH_PATH = "InmateSearch/search.action"
SEARCH_URL = urljoin(BASE_URL, SEARCH_PATH)

REQUIRED_FIELDS = {"TDCJ Number", "Name", "Unit of Assignment"}


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


async def query(
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: float | None = None,
) -> list[QueryResult]:
    """Private helper for querying TDCJ."""

    if not inmate_id and not (first_name and last_name):
        return []

    html = await _curl_search_url(last_name, first_name, inmate_id, timeout)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="tdcj_table")

    if table is None or not isinstance(table, Tag):
        return []

    for linebreak in table.find_all("br"):
        linebreak.replace_with(" ")

    header_tag = table.find("thead")
    body_tag = table.find("tbody")

    if (
        header_tag is None
        or body_tag is None
        or not isinstance(header_tag, Tag)
        or not isinstance(body_tag, Tag)
    ):
        return []

    header = header_tag.find("tr")
    if header is None or not isinstance(header, Tag):
        return []

    keys = [th.get_text(" ", strip=True) for th in header.find_all("th")]
    if not set(keys).issuperset(REQUIRED_FIELDS):
        raise ProviderError("all required fields not found")

    rows = body_tag.find_all("tr")

    def row_to_inmate(row: Tag):
        """Convert TDCJ table row to an inmate model."""

        cells = row.find_all(["th", "td"])
        values = [c.get_text(" ", strip=True) for c in cells]
        if not values:
            return None

        entry = dict(zip(keys, values))
        anchor = row.find("a")
        href = anchor.get("href") if isinstance(anchor, Tag) else None

        def parse_inmate_id(inmate_id: str) -> int:
            return int(re.sub(r"\D", "", inmate_id))

        inmate_id = parse_inmate_id(entry["TDCJ Number"])

        name = HumanName(entry.get("Name", ""))
        first: str = name.first
        last: str = name.last

        url = urljoin(BASE_URL, str(href)) if href else None

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

    return [inmate for row in rows if (inmate := row_to_inmate(row)) is not None]
