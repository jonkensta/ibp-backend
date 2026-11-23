"""Inmate search: by ID or name, upserting provider results first."""

# pylint: disable=redefined-outer-name,unused-argument

import asyncio
import datetime

import pytest
from ibp import models, providers

NOW = datetime.datetime.now(datetime.timezone.utc)


def _result(**overrides) -> providers.QueryResult:
    values = {
        "id": 12345,
        "jurisdiction": "Texas",
        "first_name": "JANE",
        "last_name": "DOE",
        "unit": "Alpha",
        "datetime_fetched": NOW,
    }
    values.update(overrides)
    return providers.QueryResult(**values)


@pytest.fixture
def seeded(api_db):
    """Seed a unit and a few cached inmates."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                city="Austin",
                state="TX",
                zipcode="78701",
            )
        )
        for jurisdiction, id_, first, last in [
            ("Texas", 11111, "MARIA", "GARCIA"),
            ("Texas", 22222, "GARCIA", "MARIANO"),
            ("Federal", 3333333, "MARIA", "GARCIAS"),
            ("Texas", 44444, "JOHN", "SMITH"),
        ]:
            session.add(
                models.Inmate(
                    jurisdiction=jurisdiction,
                    id=id_,
                    first_name=first,
                    last_name=last,
                    datetime_fetched=NOW,
                )
            )
    return api_db


@pytest.fixture
def by_id(monkeypatch):
    """Fake provider ID query; set ``.responses`` / ``.errors``, see ``.calls``."""

    async def query(id_, timeout):
        query.calls.append(id_)
        return providers.Results(query.responses, query.errors, [])

    query.calls, query.responses, query.errors = [], [], []
    monkeypatch.setattr(providers, "query_by_inmate_id", query)
    return query


@pytest.fixture
def by_name(monkeypatch):
    """Fake provider name query.

    Set ``.responses`` / ``.errors`` / ``.incomplete``, see ``.calls``.
    """

    async def query(first, last, timeout):
        query.calls.append((first, last))
        return providers.Results(query.responses, query.errors, query.incomplete)

    query.calls, query.responses, query.errors, query.incomplete = [], [], [], []
    monkeypatch.setattr(providers, "query_by_name", query)
    return query


def _search(client, query):
    response = client.get("/inmates", params={"query": query})
    assert response.status_code == 200, response.text
    return response.json()


def test_id_search_upserts_provider_result(client, seeded, by_id):
    """A provider hit is saved, linked to its known unit, and returned."""
    by_id.responses = [_result()]

    data = _search(client, "12345")

    assert by_id.calls == [12345]
    assert data["errors"] == []
    (inmate,) = data["inmates"]
    assert (inmate["jurisdiction"], inmate["id"]) == ("Texas", 12345)
    assert inmate["last_name"] == "DOE"
    with seeded() as session:
        assert session.get(models.Inmate, ("Texas", 12345)).unit_name == "Alpha"


def test_id_search_accepts_dashes(client, seeded, by_id):
    """IDs may be typed with dashes, as printed on some envelopes."""
    _search(client, "123-45")
    assert by_id.calls == [12345]


def test_id_search_unknown_unit_left_unset(client, seeded, by_id):
    """A provider unit not in the units table is not linked (no FK error)."""
    by_id.responses = [_result(unit="Nowhere")]
    _search(client, "12345")
    with seeded() as session:
        assert session.get(models.Inmate, ("Texas", 12345)).unit_name is None


def test_id_search_provider_error_returns_cache(client, seeded, by_id):
    """A provider failure is reported alongside the locally cached inmate."""
    by_id.errors = ["TDCJ timed out"]
    data = _search(client, "11111")
    assert data["errors"] == ["TDCJ timed out"]
    assert [inmate["id"] for inmate in data["inmates"]] == [11111]


def test_id_search_no_match(client, seeded, by_id):
    """An unknown ID yields an empty result, not an error."""
    assert _search(client, "99999") == {
        "inmates": [],
        "stale_inmates": [],
        "errors": [],
    }


def test_name_search_tries_both_orderings(client, seeded, by_name):
    """Both (first, last) assignments are queried and their matches unioned."""
    data = _search(client, "maria garcia")

    assert by_name.calls == [("maria", "garcia"), ("garcia", "maria")]
    ids = [inmate["id"] for inmate in data["inmates"]]
    # Exact last-name matches first (GARCIA), then by last name: GARCIAS
    # (prefix of first=MARIA, last=GARCIA) and MARIANO (the reversed order).
    assert ids == [11111, 3333333, 22222]
    assert 44444 not in ids


def test_name_search_last_comma_first(client, seeded, by_name):
    """ "last, first" finds the same inmates as "first last"."""
    by_comma = [inmate["id"] for inmate in _search(client, "garcia, maria")["inmates"]]
    by_space = [inmate["id"] for inmate in _search(client, "maria garcia")["inmates"]]
    assert by_comma == by_space


def test_name_search_upserts_and_dedupes_errors(client, seeded, by_name):
    """Provider results are saved; an error repeated by both passes shows once."""
    by_name.responses = [_result(id=55555, first_name="ANA", last_name="LOPEZ")]
    by_name.errors = ["FBOP unavailable"]

    data = _search(client, "ana lopez")

    assert data["errors"] == ["FBOP unavailable"]
    assert [inmate["id"] for inmate in data["inmates"]] == [55555]
    with seeded() as session:
        assert session.get(models.Inmate, ("Texas", 55555)) is not None


OLD = NOW - datetime.timedelta(days=31)


def _add_inmate(db, row, fetched):
    jurisdiction, id_, first, last = row
    with db.begin() as session:
        session.add(
            models.Inmate(
                jurisdiction=jurisdiction,
                id=id_,
                first_name=first,
                last_name=last,
                datetime_fetched=fetched,
            )
        )


@pytest.fixture
def aged(seeded):
    """Old cached matches for "maria garcia", past the cutoff or never fetched.

    66666 and 7777777 are named so that TDCJ and FBOP would return them;
    22222 and 3333333 match locally by prefix only.
    """
    _add_inmate(seeded, ("Texas", 66666, "MARIA", "GARCIA"), OLD)
    _add_inmate(seeded, ("Federal", 7777777, "MARIA", "GARCIA"), None)
    with seeded.begin() as session:
        session.get(models.Inmate, ("Texas", 22222)).datetime_fetched = OLD
        session.get(models.Inmate, ("Federal", 3333333)).datetime_fetched = None
    return seeded


def _provider_error(jurisdiction):
    error = providers.errors.ProviderError(f"{jurisdiction} unavailable")
    error.jurisdiction = jurisdiction
    return error


def _ids(inmates):
    return [inmate["id"] for inmate in inmates]


def test_name_search_sets_stale_matches_aside(client, aged, by_name):
    """Old matches the providers would have returned, and did not, are stale."""
    data = _search(client, "maria garcia")
    assert _ids(data["inmates"]) == [11111, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [7777777, 66666]


def test_name_search_prefix_last_name_is_not_stale(client, aged, by_name):
    """TDCJ wants the exact last name, so it never returns GARCIA-LOPEZ here."""
    _add_inmate(aged, ("Texas", 88888, "MARIA", "GARCIA-LOPEZ"), OLD)
    data = _search(client, "maria garcia")
    assert 88888 in _ids(data["inmates"])
    assert 88888 not in _ids(data["stale_inmates"])


def test_name_search_prefix_first_name(client, aged, by_name):
    """TDCJ matches a first-name prefix, so its old row is stale; FBOP doesn't."""
    data = _search(client, "mar garcia")
    assert _ids(data["inmates"]) == [11111, 7777777, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [66666]


def test_name_search_initial_never_makes_federal_stale(client, aged, by_name):
    """FBOP finds nobody by an initial; TDCJ does, as a first-name prefix."""
    data = _search(client, "m garcia")
    assert 7777777 in _ids(data["inmates"])
    assert _ids(data["stale_inmates"]) == [66666]


def test_name_search_recent_match_is_not_stale(client, seeded, by_name):
    """A match fetched within the grace period stays in the results."""
    recent = NOW - datetime.timedelta(days=29)
    _add_inmate(seeded, ("Texas", 66666, "MARIA", "GARCIA"), recent)

    data = _search(client, "maria garcia")
    assert 66666 in _ids(data["inmates"])
    assert data["stale_inmates"] == []


def test_name_search_provider_result_refreshes_old_match(client, aged, by_name):
    """A match the provider returns is fresh again, so it is not stale."""
    by_name.responses = [
        _result(id=66666, first_name="MARIA", last_name="GARCIA", unit="Alpha")
    ]
    data = _search(client, "maria garcia")
    assert _ids(data["inmates"]) == [11111, 66666, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [7777777]


def test_name_search_failed_provider_keeps_its_old_matches(client, aged, by_name):
    """With TDCJ down its old matches show (cache); FBOP's are still stale."""
    by_name.errors = [_provider_error("Texas")]
    data = _search(client, "maria garcia")
    assert _ids(data["inmates"]) == [11111, 66666, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [7777777]


def test_name_search_capped_provider_keeps_its_old_matches(client, aged, by_name):
    """A capped TDCJ answer may have left its old matches out: none is stale."""
    by_name.incomplete = ["Texas"]
    data = _search(client, "maria garcia")
    assert data["errors"] == []
    assert _ids(data["inmates"]) == [11111, 66666, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [7777777]


def test_name_search_capped_fbop_keeps_its_old_matches(client, aged, by_name):
    """A capped FBOP answer keeps FBOP's old rows; TDCJ's are still stale."""
    by_name.incomplete = ["Federal"]
    data = _search(client, "maria garcia")
    assert data["errors"] == []
    assert _ids(data["inmates"]) == [11111, 7777777, 3333333, 22222]
    assert _ids(data["stale_inmates"]) == [66666]


def test_name_search_unattributed_error_keeps_all_matches(client, aged, by_name):
    """An error that names no provider could be anyone's: nothing is stale."""
    by_name.errors = ["something broke"]
    data = _search(client, "maria garcia")
    assert _ids(data["inmates"]) == [11111, 7777777, 66666, 3333333, 22222]
    assert data["stale_inmates"] == []


def test_name_search_orders_same_name_by_freshness(client, seeded, by_name):
    """With FBOP down, its old same-name match shows but below the fresh one."""
    with seeded.begin() as session:
        session.add(
            models.Inmate(
                jurisdiction="Federal",
                id=5555555,
                first_name="MARIA",
                last_name="GARCIA",
                datetime_fetched=OLD,
            )
        )
    by_name.errors = [_provider_error("Federal")]

    data = _search(client, "maria garcia")
    assert _ids(data["inmates"])[:2] == [11111, 5555555]


def test_id_search_returns_old_match(client, aged, by_id):
    """An ID search returns its inmate however old the data is."""
    data = _search(client, "22222")
    assert _ids(data["inmates"]) == [22222]
    assert data["stale_inmates"] == []


def test_provider_errors_carry_their_jurisdiction(monkeypatch):
    """The query wrapper tags each error with the provider that raised it."""

    async def fail(**kwargs):
        raise providers.errors.ProviderError("down")

    async def empty(**kwargs):
        return providers.Answer([])

    monkeypatch.setattr(providers.base.tdcj, "query", fail)
    monkeypatch.setattr(providers.base.fbop, "query", empty)

    # pylint: disable-next=no-value-for-parameter
    query = providers.query_by_name("maria", "garcia", timeout=1)
    results = asyncio.run(query)
    assert [error.jurisdiction for error in results.errors] == ["Texas"]
    assert results.incomplete == []


def test_capped_answers_are_reported_apart_from_errors(monkeypatch):
    """A capped answer is kept and its jurisdiction listed, not made an error."""

    async def capped(**kwargs):
        return providers.Answer([_result(jurisdiction="Federal")], complete=False)

    async def empty(**kwargs):
        return providers.Answer([])

    monkeypatch.setattr(providers.base.tdcj, "query", empty)
    monkeypatch.setattr(providers.base.fbop, "query", capped)

    # pylint: disable-next=no-value-for-parameter
    query = providers.query_by_name("maria", "garcia", timeout=1)
    results = asyncio.run(query)
    assert [inmate.jurisdiction for inmate in results.inmates] == ["Federal"]
    assert results.errors == []
    assert results.incomplete == ["Federal"]


@pytest.mark.parametrize("query", ["garcia", "   "])
def test_name_search_needs_two_parts(client, seeded, by_name, query):
    """A lone name is rejected without querying providers."""
    response = client.get("/inmates", params={"query": query})
    assert response.status_code == 400
    assert "first and last name" in response.json()["detail"]
    assert not by_name.calls


def test_search_requires_query(client, seeded):
    """The query parameter is required."""
    assert client.get("/inmates").status_code == 422
