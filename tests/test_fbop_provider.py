"""FBOP provider: an answer at FBOP's record cap may be cut off."""

import asyncio

import pytest

from ibp.providers import fbop


def _record(number: int, facility: str = "BML") -> dict:
    return {
        "inmateNum": f"{number:05d}-479",
        "nameFirst": "JANE",
        "nameLast": "DOE",
        "faclCode": facility,
        "race": "White",
        "gender": "Female",
        "actRelDate": "",
        "projRelDate": "01/01/2099",
    }


def run_query(monkeypatch, records: list[dict]):
    """Run a name query against a faked FBOP answer of ``records``."""

    async def fake_curl(*_args, **_kwargs):
        return {"InmateLocator": records}

    monkeypatch.setattr(fbop, "_curl_search_url", fake_curl)
    return asyncio.run(fbop.query(first_name="jane", last_name="doe"))


def test_answer_under_the_cap_is_complete(monkeypatch):
    """Fewer records than the cap is everyone FBOP has under that name."""
    answer = run_query(monkeypatch, [_record(n) for n in range(fbop.MAX_RECORDS - 1)])
    assert answer.complete
    assert len(answer.inmates) == fbop.MAX_RECORDS - 1


@pytest.mark.parametrize("extra", [0, 1])
def test_answer_at_the_cap_is_incomplete(monkeypatch, extra):
    """At the cap, FBOP may have cut its answer off."""
    answer = run_query(
        monkeypatch, [_record(n) for n in range(fbop.MAX_RECORDS + extra)]
    )
    assert not answer.complete


def test_cap_counts_records_before_filtering(monkeypatch):
    """Records outside Texas still count toward the cap, though dropped."""
    records = [_record(0)] + [
        _record(n, facility="ATL") for n in range(1, fbop.MAX_RECORDS)
    ]
    answer = run_query(monkeypatch, records)
    assert not answer.complete
    assert [inmate.id for inmate in answer.inmates] == [479]
