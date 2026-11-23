"""TDCJ inmate query implementation."""

import asyncio
import datetime
import logging
import re
import typing
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from nameparser import HumanName  # type: ignore[import]

from .errors import ProviderError
from .misc import run_curl_exec
from .normalize import normalize_race, normalize_sex
from .types import Answer, QueryResult

LOGGER: logging.Logger = logging.getLogger(__name__)

BASE_URL = "https://inmate.tdcj.texas.gov"
SEARCH_PATH = "InmateSearch/search.action"
SEARCH_URL = urljoin(BASE_URL, SEARCH_PATH)

REQUIRED_FIELDS = {"TDCJ Number", "Name", "Unit of Assignment"}

# What TDCJ's results page says when a search matched nobody. A page with
# neither this nor a results table (an error or maintenance page) is an error,
# not "no such inmate": shipping treats an inmate TDCJ doesn't list as gone.
NO_RESULTS_TEXT = "No Inmates were found matching the search criteria"

# TDCJ lists 50 results a page, sorted by name. A search reads at most
# MAX_PAGES of them; an answer with more pages is reported as incomplete.
PAGE_SIZE = 50
MAX_PAGES = 10


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
        # An HTTP error status fails the query instead of parsing as no results.
        "--fail",
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


async def _curl_results_page(
    last_search: str,
    page: int,
    page_count: int,
    timeout: float | None = None,
):
    """Fetch a later page of a search's results.

    ``last_search`` is the search as the first page's pagination form echoes
    it back; TDCJ serves any page from it, with no session needed.
    """
    args = [
        "--ipv4",
        "--fail",
        "--data-urlencode",
        f"lastSearch={last_search}",
        "-d",
        f"currentPage={page}",
        "-d",
        f"totalPageCount={page_count}",
        SEARCH_URL,
    ]

    return await run_curl_exec(args, timeout=timeout)


def _hidden_value(soup: BeautifulSoup, name: str) -> str | None:
    """Return the value of the results page's hidden input ``name``, if any."""
    tag = soup.find("input", attrs={"name": name})
    value = tag.get("value") if isinstance(tag, Tag) else None
    return value if isinstance(value, str) else None


def _page_count(soup: BeautifulSoup) -> int | None:
    """Return how many pages TDCJ split the results into (0 means one page)."""
    try:
        return int(_hidden_value(soup, "totalPageCount") or "")
    except ValueError:
        return None


async def _later_page(
    last_search: str, page: int, page_count: int, timeout: float | None
) -> list[QueryResult]:
    """Fetch and parse results page ``page``, which must hold results."""
    html = await _curl_results_page(last_search, page, page_count, timeout)
    soup = BeautifulSoup(html, "html.parser")
    inmates = _parse_results(soup)
    if not inmates or _hidden_value(soup, "currentPage") != str(page):
        raise ProviderError(f"TDCJ did not return results page {page}")
    return inmates


async def query(
    last_name: str = "",
    first_name: str = "",
    inmate_id: str = "",
    timeout: float | None = None,
) -> Answer:
    """Private helper for querying TDCJ.

    Results split into pages are read up to :data:`MAX_PAGES`, the later
    pages concurrently; a page that fails fails the whole query. Past that
    limit, or if a full page comes with no page count, the answer is
    incomplete.
    """

    if not inmate_id and not (first_name and last_name):
        return Answer([])

    html = await _curl_search_url(last_name, first_name, inmate_id, timeout)
    soup = BeautifulSoup(html, "html.parser")
    inmates = _parse_results(soup)

    page_count = _page_count(soup)
    if page_count is None:
        return Answer(inmates, complete=len(inmates) < PAGE_SIZE)
    if page_count <= 1:
        return Answer(inmates)

    last_search = _hidden_value(soup, "lastSearch")
    if not last_search:
        raise ProviderError("TDCJ split its results into pages but gave no search")

    later_pages = await asyncio.gather(
        *(
            _later_page(last_search, page, page_count, timeout)
            for page in range(2, min(page_count, MAX_PAGES) + 1)
        )
    )
    for page_inmates in later_pages:
        inmates.extend(page_inmates)

    return Answer(inmates, complete=page_count <= MAX_PAGES)


def _parse_results(soup: BeautifulSoup) -> list[QueryResult]:
    """Parse a results page's table; raise on a page that is neither."""
    table = soup.find("table", class_="tdcj_table")

    if table is None or not isinstance(table, Tag):
        if NO_RESULTS_TEXT.lower() in soup.get_text(" ", strip=True).lower():
            return []
        raise ProviderError("TDCJ returned a page with no results table")

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
            race=normalize_race("Texas", entry.get("Race")),
            sex=normalize_sex("Texas", entry.get("Gender")),
            url=url,
            release=release,
            datetime_fetched=datetime.datetime.now(datetime.timezone.utc),
        )

    return [inmate for row in rows if (inmate := row_to_inmate(row)) is not None]
