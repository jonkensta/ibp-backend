"""TDCJ provider: telling "no such inmate" from a page it can't read."""

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
    async def fake_curl(*_args, **_kwargs):
        return page.encode()

    monkeypatch.setattr(tdcj, "_curl_search_url", fake_curl)
    return asyncio.run(tdcj.query(inmate_id="00012345"))


def test_no_results_page_is_an_empty_result(monkeypatch):
    assert run_query(monkeypatch, NO_RESULTS_PAGE) == []


def test_unrecognized_page_is_an_error(monkeypatch):
    with pytest.raises(ProviderError):
        run_query(monkeypatch, MAINTENANCE_PAGE)


def test_results_page_is_parsed(monkeypatch):
    [inmate] = run_query(monkeypatch, RESULTS_PAGE)
    assert (inmate.id, inmate.unit) == (12345, "Gamma")
