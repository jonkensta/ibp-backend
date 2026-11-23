"""TDCJ provider: reading its results pages, and pages it can't read."""

import asyncio

import pytest

from ibp.providers import tdcj
from ibp.providers.errors import ProviderError

NO_RESULTS_PAGE = """<html><body><div class="content">
<p>No Inmates were found matching the search criteria.</p>
</div></body></html>"""

MAINTENANCE_PAGE = """<html><body><h1>Service Unavailable</h1>
<p>The site is down for maintenance.</p></body></html>"""

RESULTS_PAGE = """<html><body><table class="tdcj_table">
<thead><tr><th>Name</th><th>TDCJ Number</th><th>Race</th><th>Gender</th>
<th>Projected Release Date</th><th>Unit of Assignment</th></tr></thead>
<tbody><tr><td><a href="/InmateSearch/viewDetail.action?sid=1">DOE, JANE</a></td>
<td>00012345</td><td>W</td><td>F</td><td>2030-01-01</td><td>Gamma</td></tr></tbody>
</table></body></html>"""


def run_query(monkeypatch, page: str):
    """Run an ID query against a faked TDCJ results ``page``."""

    async def fake_curl(*_args, **_kwargs):
        return page.encode()

    monkeypatch.setattr(tdcj, "_curl_search_url", fake_curl)
    answer = asyncio.run(tdcj.query(inmate_id="00012345"))
    assert answer.complete
    return answer.inmates


def test_no_results_page_is_an_empty_result(monkeypatch):
    """TDCJ's "no inmates found" page means nobody matched."""
    assert run_query(monkeypatch, NO_RESULTS_PAGE) == []


def test_unrecognized_page_is_an_error(monkeypatch):
    """A page with neither results nor "no inmates found" is an error."""
    with pytest.raises(ProviderError):
        run_query(monkeypatch, MAINTENANCE_PAGE)


def test_results_page_is_parsed(monkeypatch):
    """Each results row becomes an inmate."""
    [inmate] = run_query(monkeypatch, RESULTS_PAGE)
    assert (inmate.id, inmate.unit) == (12345, "Gamma")


LAST_SEARCH = '{"tdcj":"","lastName":"Doe","firstName":"Jane","race":"ALL"}'


def results_page(page: int, rows: int, page_count: int | None) -> str:
    """Build results page ``page`` of ``page_count`` with ``rows`` made-up rows.

    ``page_count`` of ``None`` leaves out the pagination form.
    """
    body = "".join(
        f"<tr><td><a href='/x?sid={page}{row}'>DOE, JANE</a></td>"
        f"<td>{page * 1000 + row:08d}</td><td>W</td><td>F</td>"
        "<td>2030-01-01</td><td>Gamma</td></tr>"
        for row in range(rows)
    )
    form = (
        ""
        if page_count is None
        else "<form id='form_paginate' action='/InmateSearch/search.action'>"
        f"<input type='hidden' name='lastSearch' value='{LAST_SEARCH}'/>"
        f"<input type='hidden' name='currentPage' value='{page}'/>"
        f"<input type='hidden' name='totalPageCount' value='{page_count}'/>"
        "</form>"
    )
    return f"""<html><body><table class="tdcj_table">
<thead><tr><th>Name</th><th>TDCJ Number</th><th>Race</th><th>Gender</th>
<th>Projected Release Date</th><th>Unit of Assignment</th></tr></thead>
<tbody>{body}</tbody></table>{form}</body></html>"""


def run_name_query(monkeypatch, first_page: str, later_page):
    """Run a name query; ``later_page(page, page_count)`` serves later pages.

    Returns the answer and the (page, page_count) of each later page asked for.
    """
    calls = []

    async def fake_search(*_args, **_kwargs):
        return first_page.encode()

    async def fake_page(last_search, page, page_count, timeout=None):
        assert last_search == LAST_SEARCH
        calls.append((page, page_count))
        return later_page(page, page_count).encode()

    monkeypatch.setattr(tdcj, "_curl_search_url", fake_search)
    monkeypatch.setattr(tdcj, "_curl_results_page", fake_page)
    answer = asyncio.run(tdcj.query(first_name="jane", last_name="doe"))
    return answer, sorted(calls)


def full_pages(last_rows: int):
    """Serve full later pages, with ``last_rows`` rows on the last one."""

    def page(number: int, page_count: int) -> str:
        rows = last_rows if number == page_count else tdcj.PAGE_SIZE
        return results_page(number, rows, page_count)

    return page


def test_single_page_is_complete(monkeypatch):
    """TDCJ gives a page count of 0 for one page; no more pages are fetched."""
    answer, calls = run_name_query(monkeypatch, results_page(1, 3, 0), full_pages(0))
    assert (len(answer.inmates), answer.complete, calls) == (3, True, [])


def test_every_page_is_read(monkeypatch):
    """Rows past the first page are found too."""
    answer, calls = run_name_query(
        monkeypatch, results_page(1, tdcj.PAGE_SIZE, 3), full_pages(7)
    )
    assert calls == [(2, 3), (3, 3)]
    assert answer.complete
    ids = [inmate.id for inmate in answer.inmates]
    assert len(ids) == len(set(ids)) == 2 * tdcj.PAGE_SIZE + 7


def test_pages_past_the_limit_are_incomplete(monkeypatch):
    """Only MAX_PAGES pages are read, and the answer says it is cut off."""
    page_count = tdcj.MAX_PAGES + 2
    answer, calls = run_name_query(
        monkeypatch, results_page(1, tdcj.PAGE_SIZE, page_count), full_pages(1)
    )
    assert [page for page, _ in calls] == list(range(2, tdcj.MAX_PAGES + 1))
    assert len(answer.inmates) == tdcj.MAX_PAGES * tdcj.PAGE_SIZE
    assert not answer.complete


def test_full_page_without_page_count_is_incomplete(monkeypatch):
    """A full page with no pagination form may have more behind it."""
    answer, calls = run_name_query(
        monkeypatch, results_page(1, tdcj.PAGE_SIZE, None), full_pages(0)
    )
    assert (len(answer.inmates), answer.complete, calls) == (tdcj.PAGE_SIZE, False, [])


def test_failed_later_page_fails_the_query(monkeypatch):
    """A later page that fails fails the query, not a short answer."""

    def page(number: int, page_count: int) -> str:
        if number == 3:
            raise RuntimeError("curl command failed with exit code 22")
        return results_page(number, tdcj.PAGE_SIZE, page_count)

    with pytest.raises(RuntimeError):
        run_name_query(monkeypatch, results_page(1, tdcj.PAGE_SIZE, 4), page)


@pytest.mark.parametrize(
    "bad_page",
    [
        NO_RESULTS_PAGE,
        MAINTENANCE_PAGE,
        results_page(2, 1, 0).replace("value='2'", "value='1'"),
    ],
    ids=["no-results", "unreadable", "wrong-page"],
)
def test_later_page_without_its_results_fails_the_query(monkeypatch, bad_page):
    """A later page must hold that page's results."""
    with pytest.raises(ProviderError):
        run_name_query(
            monkeypatch,
            results_page(1, tdcj.PAGE_SIZE, 2),
            lambda number, page_count: bad_page,
        )
