"""Tests for the read-only request address endpoints used by shippy."""

# pylint: disable=redefined-outer-name

import datetime

import pytest
from ibp import api, models

# Fresh, so the (contrasting) inmate view does not try to re-scrape.
FETCHED = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)


@pytest.fixture
def seeded(api_db):
    """Seed one unit, an inmate with requests, and an inmate without a unit."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                street2="Bldg 2",
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
                datetime_fetched=FETCHED,
            )
        )
        session.add(
            models.Inmate(
                jurisdiction="Federal",
                id=7654321,
                first_name="JOHN",
                last_name="ROE",
                unit_name=None,
                datetime_fetched=FETCHED,
            )
        )
        for inmate_jurisdiction, inmate_id, index, action, request_id in [
            ("Texas", 12345, 0, "Filled", 501),
            ("Texas", 12345, 1, "Tossed", None),
            ("Federal", 7654321, 0, "Filled", 502),
        ]:
            session.add(
                models.Request(
                    inmate_jurisdiction=inmate_jurisdiction,
                    inmate_id=inmate_id,
                    index=index,
                    action=action,
                    request_id=request_id,
                    date_processed=datetime.date(2026, 10, 2),
                    date_postmarked=datetime.date(2026, 9, 28),
                )
            )
    return api_db


@pytest.fixture
def no_side_effects(seeded, monkeypatch):
    """Fail on any provider upsert and check no lookup rows are written."""

    def no_upsert(*_args, **_kwargs):
        raise AssertionError("provider upsert must not be called")

    monkeypatch.setattr(api, "upsert_inmates_by_inmate_id", no_upsert)
    monkeypatch.setattr(api, "upsert_inmates_by_name", no_upsert)

    def lookup_count() -> int:
        with seeded() as session:
            return session.query(models.Lookup).count()

    before = lookup_count()
    yield
    assert lookup_count() == before


EXPECTED_ADDRESS = {
    "name": "Jane Doe #00012345",
    "first_name": "JANE",
    "last_name": "DOE",
    "jurisdiction": "Texas",
    "id": 12345,
    "datetime_fetched": FETCHED.isoformat(),
    "unit_name": "Alpha",
    "street1": "1 Main St",
    "street2": "Bldg 2",
    "city": "Austin",
    "state": "TX",
    "zipcode": "78701",
    "shipping_method": "Box",
}


@pytest.mark.usefixtures("no_side_effects")
def test_request_address(client):
    """The address of an existing request is returned with its inmate's unit."""
    response = client.get("/inmates/Texas/12345/requests/0/address")
    assert response.status_code == 200
    assert response.json() == {
        **EXPECTED_ADDRESS,
        "request_index": 0,
        "request_id": 501,
        "action": "Filled",
    }


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_tossed(client):
    """Tossed requests are still addressable; the action is reported."""
    response = client.get("/inmates/Texas/12345/requests/1/address")
    assert response.status_code == 200
    assert response.json()["action"] == "Tossed"
    assert response.json()["request_id"] is None


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_unknown_inmate(client):
    """An unknown inmate is a 404 that names the inmate."""
    response = client.get("/inmates/Texas/99999999/requests/0/address")
    assert response.status_code == 404
    assert response.json()["detail"] == "Texas inmate #99999999 not found."


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_unknown_request(client):
    """An unknown request index for a known inmate is a 404."""
    response = client.get("/inmates/Texas/12345/requests/7/address")
    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Request #7 not found for Texas inmate #00012345."
    )


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_inmate_without_unit(client):
    """An inmate with no unit cannot be addressed: 409, not 500."""
    response = client.get("/inmates/Federal/7654321/requests/0/address")
    assert response.status_code == 409
    assert "not assigned to a unit" in response.json()["detail"]


@pytest.mark.usefixtures("no_side_effects")
def test_legacy_request_address(client):
    """A legacy request id resolves to the same address."""
    response = client.get("/inmates/by-request/501/address")
    assert response.status_code == 200
    assert response.json() == {
        **EXPECTED_ADDRESS,
        "request_index": 0,
        "request_id": 501,
        "action": "Filled",
    }


@pytest.mark.usefixtures("no_side_effects")
def test_legacy_request_address_unknown(client):
    """An unknown legacy request id is a 404."""
    response = client.get("/inmates/by-request/999/address")
    assert response.status_code == 404
    assert response.json()["detail"] == "No request found with legacy request_id 999"


@pytest.mark.usefixtures("no_side_effects")
def test_legacy_request_address_inmate_without_unit(client):
    """A legacy request for an inmate with no unit is a 409."""
    response = client.get("/inmates/by-request/502/address")
    assert response.status_code == 409


@pytest.mark.usefixtures("no_side_effects")
def test_legacy_request_inmate_is_read_only(client):
    """The existing by-request inmate endpoint does not write or scrape either."""
    response = client.get("/inmates/by-request/501")
    assert response.status_code == 200
    assert response.json()["id"] == 12345


def test_inmate_view_records_lookup(client, seeded):
    """Contrast: the inmate view records a lookup on every call."""
    response = client.get("/inmates/Texas/12345")
    assert response.status_code == 200
    with seeded() as session:
        count = session.query(models.Lookup).count()
    assert count == 1
