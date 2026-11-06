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
    "unit_recipient_name": "ATTN: MAILROOM STAFF",
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
        "code": "TEX-12345-0",
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
        "code": "TEX-12345-0",
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


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    "url",
    ["/inmates/Texas/12345/requests/0/address", "/inmates/by-request/501/address"],
)
def test_request_address_uses_saved_unit_recipient_name(client, url):
    """The unit recipient line follows the editable shipping settings."""
    settings = client.get("/shipping-settings").json()
    settings["unit_recipient_name"] = "ATTN: MAIL ROOM"
    assert client.put("/shipping-settings", json=settings).status_code == 200

    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["unit_recipient_name"] == "ATTN: MAIL ROOM"


ADDRESS_TEX_0 = {
    **EXPECTED_ADDRESS,
    "request_index": 0,
    "request_id": 501,
    "code": "TEX-12345-0",
    "action": "Filled",
}


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "url",
    [
        "/requests/TEX-12345-0/address",
        "/requests/tex-00012345-0/address",
        "/requests/legacy/501/address",
        # Deprecated aliases (shippy).
        "/inmates/Texas/12345/requests/0/address",
        "/inmates/by-request/501/address",
    ],
)
def test_address_paths_agree(client, url):
    """Every address path returns the same address, including the code."""
    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == ADDRESS_TEX_0


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_by_code_tossed(client):
    """Tossed requests are addressable by code too."""
    response = client.get("/requests/TEX-12345-1/address")
    assert response.status_code == 200
    assert response.json()["action"] == "Tossed"
    assert response.json()["code"] == "TEX-12345-1"


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "code", ["TEX-12345-7", "TEX-99999999-0", "FED-12345-0", "TEX-12345", "junk"]
)
def test_request_address_by_code_not_found(client, code):
    """Unknown and malformed codes are one 404."""
    response = client.get(f"/requests/{code}/address")
    assert response.status_code == 404
    assert response.json()["detail"] == "Request not found."


@pytest.mark.usefixtures("no_side_effects")
def test_request_address_by_code_inmate_without_unit(client):
    """A Federal code resolves; an inmate with no unit is still a 409."""
    response = client.get("/requests/FED-7654321-0/address")
    assert response.status_code == 409
    assert "not assigned to a unit" in response.json()["detail"]


@pytest.mark.usefixtures("no_side_effects")
def test_legacy_address_unknown(client):
    """An unknown legacy id is a 404 with the same detail as the old path."""
    response = client.get("/requests/legacy/999/address")
    assert response.status_code == 404
    assert response.json()["detail"] == "No request found with legacy request_id 999"


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "url", ["/requests/legacy/501/inmate", "/inmates/by-request/501"]
)
def test_legacy_inmate(client, url):
    """A legacy id resolves to its inmate, whose requests carry their codes."""
    response = client.get(url)
    assert response.status_code == 200
    inmate = response.json()
    assert (inmate["jurisdiction"], inmate["id"]) == ("Texas", 12345)
    assert sorted(request["code"] for request in inmate["requests"]) == [
        "TEX-12345-0",
        "TEX-12345-1",
    ]


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "url", ["/requests/legacy/999/inmate", "/inmates/by-request/999"]
)
def test_legacy_inmate_unknown(client, url):
    """An unknown legacy id is a 404 on both inmate paths."""
    response = client.get(url)
    assert response.status_code == 404
    assert response.json()["detail"] == "No request found with legacy request_id 999"


def test_aliases_hidden_from_openapi(client):
    """The deprecated aliases work but are not in the OpenAPI schema."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/requests/{code}/address" in paths
    assert "/requests/legacy/{legacy_id}/inmate" in paths
    assert "/requests/legacy/{legacy_id}/address" in paths
    for alias in [
        "/inmates/by-request/{request_id}",
        "/inmates/by-request/{request_id}/address",
        "/inmates/{jurisdiction}/{inmate_id}/requests/{request_index}/address",
    ]:
        assert alias not in paths
