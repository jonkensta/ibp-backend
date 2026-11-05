"""Inmate search: by ID or name, upserting provider results first."""

# pylint: disable=redefined-outer-name,unused-argument

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
        return query.responses, query.errors

    query.calls, query.responses, query.errors = [], [], []
    monkeypatch.setattr(providers, "query_by_inmate_id", query)
    return query


@pytest.fixture
def by_name(monkeypatch):
    """Fake provider name query; set ``.responses`` / ``.errors``, see ``.calls``."""

    async def query(first, last, timeout):
        query.calls.append((first, last))
        return query.responses, query.errors

    query.calls, query.responses, query.errors = [], [], []
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
    assert _search(client, "99999") == {"inmates": [], "errors": []}


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
