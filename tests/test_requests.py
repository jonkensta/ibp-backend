"""Adding and deleting an inmate's requests."""

# pylint: disable=redefined-outer-name,unused-argument

import datetime

import pytest
from ibp import models

REQUESTS_URL = "/inmates/Texas/12345/requests"

NEW_REQUEST = {
    "date_processed": "2026-10-02",
    "date_postmarked": "2026-09-28",
    "action": "Filled",
}


@pytest.fixture
def seeded(api_db):
    """Seed one Texas inmate with no requests."""
    with api_db.begin() as session:
        session.add(
            models.Inmate(
                jurisdiction="Texas",
                id=12345,
                first_name="JANE",
                last_name="DOE",
                datetime_fetched=datetime.datetime.now(datetime.timezone.utc),
            )
        )
    return api_db


def _indices(api_db) -> list[int]:
    with api_db() as session:
        return sorted(request.index for request in session.query(models.Request).all())


def test_add_request(client, seeded):
    """A new request is stored and returned with the next free index."""
    response = client.post(REQUESTS_URL, json=NEW_REQUEST)
    assert response.status_code == 200
    assert response.json() == {
        **NEW_REQUEST,
        "request_id": None,
        "index": 0,
        "code": "TEX-00012345-0",
        "contents_revision": 0,
    }

    with seeded() as session:
        request = session.query(models.Request).one()
        assert (request.inmate_jurisdiction, request.inmate_id) == ("Texas", 12345)
        assert request.date_postmarked == datetime.date(2026, 9, 28)


def test_add_request_with_request_id(client, seeded):
    """The optional legacy request number is kept."""
    body = {**NEW_REQUEST, "action": "Tossed", "request_id": 777}
    data = client.post(REQUESTS_URL, json=body).json()
    assert data["request_id"] == 777
    assert data["action"] == "Tossed"


def test_inmate_requests_include_code(client, seeded):
    """Every request in the inmate view carries its request code."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    requests = client.get("/inmates/Texas/12345").json()["requests"]
    assert sorted(request["code"] for request in requests) == [
        "TEX-00012345-0",
        "TEX-00012345-1",
    ]


def _add(client) -> int:
    response = client.post(REQUESTS_URL, json=NEW_REQUEST)
    assert response.status_code == 200
    return response.json()["index"]


def _next_request_index(api_db) -> int:
    with api_db() as session:
        return session.get(models.Inmate, ("Texas", 12345)).next_request_index


def test_add_request_indices_count_up(client, seeded):
    """Indices count up per inmate from 0; the counter tracks the next one."""
    assert [_add(client) for _ in range(3)] == [0, 1, 2]
    assert _next_request_index(seeded) == 3


def test_add_request_never_reuses_newest_index(client, seeded):
    """Deleting the newest request (undoing a mistake) does not free its index."""
    assert [_add(client) for _ in range(3)] == [0, 1, 2]
    assert client.delete("/requests/TEX-12345-2").status_code == 204
    assert _add(client) == 3
    assert _indices(seeded) == [0, 1, 3]


def test_add_request_never_reuses_middle_index(client, seeded):
    """Deleting a middle request leaves a gap that is never filled."""
    assert [_add(client) for _ in range(3)] == [0, 1, 2]
    assert client.delete("/requests/TEX-12345-1").status_code == 204
    assert _add(client) == 3
    assert _indices(seeded) == [0, 2, 3]


def test_add_request_indices_are_per_inmate(client, seeded):
    """Each inmate has its own counter."""
    with seeded.begin() as session:
        session.add(models.Inmate(jurisdiction="Federal", id=12345))
    assert _add(client) == 0
    response = client.post("/inmates/Federal/12345/requests", json=NEW_REQUEST)
    assert response.json()["index"] == 0
    assert response.json()["code"] == "FED-00012345-0"
    assert _add(client) == 1


def test_concurrent_adds_both_succeed(seeded):
    """Two simultaneous adds for one inmate both succeed with distinct indexes.

    Two intake stations can add a request for the same inmate at once. Both
    start their transactions before either allocates an index; the index
    allocation must serialise them rather than hand out one index twice.
    """
    # pylint: disable=import-outside-toplevel
    import asyncio

    import httpx
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.util import await_only

    from ibp.base import app

    async def run() -> list[httpx.Response]:
        # Hold each add at the start of its transaction until both are there,
        # so they always overlap instead of depending on timing.
        barrier = asyncio.Barrier(2)
        waiting = {"left": 2}

        def after_begin(_session, _transaction, _connection):
            if waiting["left"]:
                waiting["left"] -= 1
                await_only(asyncio.wait_for(barrier.wait(), timeout=5))

        event.listen(Session, "after_begin", after_begin)
        try:
            # Return a 500 as a response, as production does, instead of raising.
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return list(
                    await asyncio.gather(
                        http.post(REQUESTS_URL, json=NEW_REQUEST),
                        http.post(REQUESTS_URL, json=NEW_REQUEST),
                    )
                )
        finally:
            event.remove(Session, "after_begin", after_begin)

    responses = asyncio.run(run())
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["index"] for response in responses) == [0, 1]
    assert _indices(seeded) == [0, 1]
    assert _next_request_index(seeded) == 2


def test_add_request_unknown_inmate(client, seeded):
    """Requests can only be added to a known inmate."""
    response = client.post("/inmates/Texas/99999/requests", json=NEW_REQUEST)
    assert response.status_code == 404
    response = client.post("/inmates/Federal/12345/requests", json=NEW_REQUEST)
    assert response.status_code == 404
    assert not _indices(seeded)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "Lost"},
        {"date_processed": "not a date"},
        {"date_postmarked": None},
    ],
)
def test_add_request_invalid(client, seeded, change):
    """Invalid bodies are a 422 and nothing is written."""
    response = client.post(REQUESTS_URL, json={**NEW_REQUEST, **change})
    assert response.status_code == 422
    assert not _indices(seeded)


def test_delete_request(client, seeded):
    """Deleting removes only that request; a second delete is a 404."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    client.post(REQUESTS_URL, json=NEW_REQUEST)

    response = client.delete("/requests/TEX-12345-0")
    assert response.status_code == 204
    assert not response.content
    assert _indices(seeded) == [1]

    response = client.delete("/requests/TEX-12345-0")
    assert response.status_code == 404
    assert response.json()["detail"] == "Request not found."
    assert _indices(seeded) == [1]


def test_delete_request_by_lowercase_padded_code(client, seeded):
    """The code is case-insensitive and may zero-pad the inmate number."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    assert client.delete("/requests/tex-00012345-0").status_code == 204
    assert not _indices(seeded)


@pytest.mark.parametrize(
    "code",
    [
        "TEX-00012345-5",
        "TEX-00099999-0",
        "FED-00012345-0",
        "TEX-12345",
        "XYZ-12345-0",
        "0",
    ],
)
def test_delete_request_not_found(client, seeded, code):
    """Unknown and malformed codes are 404s and delete nothing."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    response = client.delete(f"/requests/{code}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Request not found."
    assert _indices(seeded) == [0]


def test_old_delete_path_is_gone(client, seeded):
    """Deleting moved to /requests/{code}; the old inmate path deletes nothing."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    assert client.delete(f"{REQUESTS_URL}/0").status_code == 404
    assert _indices(seeded) == [0]
