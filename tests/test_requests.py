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
    assert response.json() == {**NEW_REQUEST, "request_id": None, "index": 0}

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


def test_add_request_indices_fill_gaps(client, seeded):
    """Indices count up per inmate and reuse the lowest free one."""
    for expected in range(3):
        assert client.post(REQUESTS_URL, json=NEW_REQUEST).json()["index"] == expected

    assert client.delete(f"{REQUESTS_URL}/1").status_code == 204
    assert client.post(REQUESTS_URL, json=NEW_REQUEST).json()["index"] == 1
    assert _indices(seeded) == [0, 1, 2]


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

    response = client.delete(f"{REQUESTS_URL}/0")
    assert response.status_code == 204
    assert not response.content
    assert _indices(seeded) == [1]

    assert client.delete(f"{REQUESTS_URL}/0").status_code == 404
    assert _indices(seeded) == [1]


def test_delete_request_not_found(client, seeded):
    """Unknown inmates and indices are 404s."""
    client.post(REQUESTS_URL, json=NEW_REQUEST)
    assert client.delete(f"{REQUESTS_URL}/5").status_code == 404
    assert client.delete("/inmates/Texas/99999/requests/0").status_code == 404
    assert client.delete("/inmates/Federal/12345/requests/0").status_code == 404
    assert _indices(seeded) == [0]
