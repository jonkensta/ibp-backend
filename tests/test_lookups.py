"""Lookups are recorded only by an explicit POST, never by reads or CRUD."""

# pylint: disable=redefined-outer-name

import datetime

import pytest
from ibp import api, models

FRESH = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
STALE = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)

INMATE_URL = "/inmates/Texas/12345"
LOOKUPS_URL = f"{INMATE_URL}/lookups"


@pytest.fixture
def seeded(api_db):
    """Seed one fresh Texas inmate with a unit, a request and a comment."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                city="Austin",
                state="TX",
                zipcode="78701",
                shipping_method="Box",
            )
        )
        session.add(
            models.Inmate(
                jurisdiction="Texas",
                id=12345,
                first_name="JANE",
                last_name="DOE",
                unit_name="Alpha",
                datetime_fetched=FRESH,
            )
        )
        session.add(
            models.Request(
                inmate_jurisdiction="Texas",
                inmate_id=12345,
                index=0,
                action="Filled",
                date_processed=datetime.date(2026, 10, 2),
                date_postmarked=datetime.date(2026, 9, 28),
            )
        )
        session.add(
            models.Comment(
                inmate_jurisdiction="Texas",
                inmate_id=12345,
                index=0,
                author="vol",
                body="hello",
                datetime_created=FRESH,
            )
        )
    return api_db


@pytest.fixture
def lookup_count(seeded):
    """Return a function counting the lookup rows in the database."""

    def count() -> int:
        with seeded() as session:
            return session.query(models.Lookup).count()

    return count


@pytest.fixture
def no_upsert(monkeypatch):
    """Fail if any endpoint tries to refresh an inmate from the providers."""

    def fail(*_args, **_kwargs):
        raise AssertionError("provider upsert must not be called")

    monkeypatch.setattr(api, "upsert_inmates_by_inmate_id", fail)
    monkeypatch.setattr(api, "upsert_inmates_by_name", fail)


@pytest.mark.usefixtures("no_upsert")
def test_get_inmate_is_read_only(client, lookup_count):
    """Refetching the inmate view never records a lookup."""
    for _ in range(3):
        response = client.get(INMATE_URL)
        assert response.status_code == 200
        assert response.json()["lookups"] == []
    assert lookup_count() == 0


def test_get_inmate_does_not_scrape_stale_entry(client, seeded, lookup_count):
    """A stale entry is returned as is; refreshing it is the POST's job."""
    with seeded.begin() as session:
        session.get(models.Inmate, ("Texas", 12345)).datetime_fetched = STALE
    # The api_db fixture makes any provider query fail the request.
    response = client.get(INMATE_URL)
    assert response.status_code == 200
    assert lookup_count() == 0


@pytest.mark.usefixtures("seeded")
def test_get_unknown_inmate_is_404_without_scraping(client):
    """An unknown inmate is a 404, not a provider query."""
    response = client.get("/inmates/Texas/99999")
    assert response.status_code == 404


@pytest.mark.usefixtures("no_upsert")
def test_record_lookup(client, lookup_count):
    """Each POST records exactly one lookup and returns the inmate with it."""
    response = client.post(LOOKUPS_URL)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 12345
    assert len(body["lookups"]) == 1
    assert body["requests"] and body["comments"]
    assert lookup_count() == 1

    assert len(client.get(INMATE_URL).json()["lookups"]) == 1

    client.post(LOOKUPS_URL)
    assert lookup_count() == 2


@pytest.mark.usefixtures("no_upsert")
def test_record_lookup_keeps_most_recent(client, lookup_count):
    """Only the most recent MAX_LOOKUPS_PER_INMATE lookups are kept."""
    for _ in range(api.MAX_LOOKUPS_PER_INMATE + 2):
        lookups = client.post(LOOKUPS_URL).json()["lookups"]
    assert lookup_count() == api.MAX_LOOKUPS_PER_INMATE
    assert len(lookups) == api.MAX_LOOKUPS_PER_INMATE
    created = [lookup["datetime_created"] for lookup in lookups]
    assert created == sorted(created, reverse=True)


def test_record_lookup_refreshes_stale_entry(client, seeded, lookup_count, monkeypatch):
    """Opening a stale inmate refreshes it from the providers first."""
    with seeded.begin() as session:
        session.get(models.Inmate, ("Texas", 12345)).datetime_fetched = STALE

    calls = []

    async def fake_upsert(_session, inmate_id):
        calls.append(inmate_id)
        return []

    monkeypatch.setattr(api, "upsert_inmates_by_inmate_id", fake_upsert)

    assert client.post(LOOKUPS_URL).status_code == 201
    assert calls == [12345]
    assert lookup_count() == 1


def test_record_lookup_unknown_inmate(client, lookup_count, monkeypatch):
    """An inmate the providers cannot find is a 404 with nothing recorded."""

    async def fake_upsert(_session, _inmate_id):
        return []

    monkeypatch.setattr(api, "upsert_inmates_by_inmate_id", fake_upsert)

    assert client.post("/inmates/Texas/99999/lookups").status_code == 404
    assert lookup_count() == 0


@pytest.mark.usefixtures("no_upsert")
def test_crud_and_reads_do_not_record_lookups(client, lookup_count):
    """Requests, comments, labels, warnings and addresses never add lookups."""
    request = {
        "date_processed": "2026-11-01",
        "date_postmarked": "2026-10-30",
        "action": "Filled",
    }
    assert client.post(LOOKUPS_URL).status_code == 201

    calls = [
        client.post(f"{INMATE_URL}/requests/validate", json=request),
        client.post(f"{INMATE_URL}/requests", json=request),
        client.get(f"{INMATE_URL}/requests/1/label"),
        client.get(f"{INMATE_URL}/requests/1/address"),
        client.delete(f"{INMATE_URL}/requests/1"),
        client.post(f"{INMATE_URL}/comments", json={"author": "a", "body": "b"}),
        client.delete(f"{INMATE_URL}/comments/1"),
        client.get(f"{INMATE_URL}/warnings"),
        client.get(INMATE_URL),
    ]
    assert all(response.is_success for response in calls), [
        (response.request.url, response.status_code) for response in calls
    ]
    assert lookup_count() == 1


@pytest.mark.usefixtures("no_upsert")
def test_concurrent_lookups_both_succeed(seeded, lookup_count):
    """Two simultaneous POSTs for one inmate both record a lookup, never a 500.

    The frontend can open the same inmate twice at once (e.g. GlobalSearch's
    auto-redirect firing twice). Both requests pick the next free lookup index
    before either commits; the loser must retry, not fail on the unique key.
    """
    # pylint: disable=import-outside-toplevel
    import asyncio

    import httpx
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.util import await_only

    from ibp.base import app

    async def run() -> list[httpx.Response]:
        # Hold the first flush of each request until both have chosen an
        # index, so they always collide (as in production) instead of
        # depending on timing.
        barrier = asyncio.Barrier(2)
        waiting = {"left": 2}

        def before_flush(session, _context, _instances):
            if waiting["left"] and any(
                isinstance(obj, models.Lookup) for obj in session.new
            ):
                waiting["left"] -= 1
                await_only(asyncio.wait_for(barrier.wait(), timeout=5))

        event.listen(Session, "before_flush", before_flush)
        try:
            # Return a 500 as a response, as production does, instead of raising.
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return list(
                    await asyncio.gather(http.post(LOOKUPS_URL), http.post(LOOKUPS_URL))
                )
        finally:
            event.remove(Session, "before_flush", before_flush)

    responses = asyncio.run(run())
    assert [response.status_code for response in responses] == [201, 201]
    assert lookup_count() == 2
    with seeded() as session:
        indices = sorted(lookup.index for lookup in session.query(models.Lookup))
    assert indices == [0, 1]
