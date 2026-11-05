"""Tests for the unit endpoints."""

# pylint: disable=redefined-outer-name

import pytest
from ibp import models

NEW_UNIT = {
    "name": "Gamma",
    "jurisdiction": "Texas",
    "street1": "3 Main St",
    "street2": None,
    "city": "Houston",
    "state": "TX",
    "zipcode": "77002",
    "url": None,
    "shipping_method": "Box",
}


@pytest.fixture
def seeded(api_db):
    """Seed one Texas unit."""
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
    return api_db


def test_list_units(client, seeded):  # pylint: disable=unused-argument
    """GET /units lists every unit."""
    response = client.get("/units")
    assert response.status_code == 200
    assert [unit["name"] for unit in response.json()] == ["Alpha"]


def test_get_unit(client, seeded):  # pylint: disable=unused-argument
    """GET /units/{jurisdiction}/{name} returns the unit."""
    response = client.get("/units/Texas/Alpha")
    assert response.status_code == 200
    assert response.json()["city"] == "Austin"


def test_get_unit_not_found(client, seeded):  # pylint: disable=unused-argument
    """An unknown unit is a 404; the jurisdiction is part of the key."""
    assert client.get("/units/Texas/Nope").status_code == 404
    assert client.get("/units/Federal/Alpha").status_code == 404


def test_get_unit_bad_jurisdiction(client, seeded):  # pylint: disable=unused-argument
    """A jurisdiction outside the enum is a 422."""
    assert client.get("/units/Mars/Alpha").status_code == 422


def test_create_unit(client, seeded):
    """POST /units creates the unit and returns it with a 201."""
    response = client.post("/units", json=NEW_UNIT)
    assert response.status_code == 201
    assert response.json() == NEW_UNIT

    assert client.get("/units/Texas/Gamma").json() == NEW_UNIT
    with seeded() as session:
        assert session.get(models.Unit, ("Texas", "Gamma")) is not None


def test_create_unit_optional_fields_default(client, api_db):
    """street2, url and shipping_method may be omitted."""
    body = {
        key: value
        for key, value in NEW_UNIT.items()
        if key not in ("street2", "url", "shipping_method")
    }
    response = client.post("/units", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["street2"] is None
    assert data["url"] is None
    assert data["shipping_method"] is None
    with api_db() as session:
        assert session.get(models.Unit, ("Texas", "Gamma")) is not None


def test_create_unit_duplicate(client, seeded):
    """Creating a unit that already exists is a 409 and changes nothing."""
    body = {**NEW_UNIT, "name": "Alpha", "city": "Elsewhere"}
    response = client.post("/units", json=body)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    with seeded() as session:
        assert session.get(models.Unit, ("Texas", "Alpha")).city == "Austin"


def test_create_unit_same_name_other_jurisdiction(
    client, seeded
):  # pylint: disable=unused-argument
    """Units are unique by (jurisdiction, name), not by name alone."""
    body = {**NEW_UNIT, "name": "Alpha", "jurisdiction": "Federal"}
    assert client.post("/units", json=body).status_code == 201
    assert client.get("/units/Federal/Alpha").status_code == 200
    assert client.get("/units/Texas/Alpha").json()["city"] == "Austin"


@pytest.mark.parametrize(
    "change",
    [
        {"jurisdiction": "Mars"},
        {"shipping_method": "Pigeon"},
        {"zipcode": "123"},
        {"state": "T"},
        {"name": ""},
        {"street1": ""},
        {"city": ""},
    ],
)
def test_create_unit_invalid(client, api_db, change):
    """Invalid fields are a 422 and nothing is written."""
    response = client.post("/units", json={**NEW_UNIT, **change})
    assert response.status_code == 422
    with api_db() as session:
        assert session.query(models.Unit).count() == 0


@pytest.mark.parametrize("missing", ["name", "jurisdiction", "street1", "city"])
def test_create_unit_missing_field(client, api_db, missing):
    """Required fields must be present."""
    body = {key: value for key, value in NEW_UNIT.items() if key != missing}
    assert client.post("/units", json=body).status_code == 422
    with api_db() as session:
        assert session.query(models.Unit).count() == 0


def test_update_unit(client, seeded):  # pylint: disable=unused-argument
    """PUT updates only the given fields."""
    response = client.put("/units/Texas/Alpha", json={"city": "Round Rock"})
    assert response.status_code == 200
    data = response.json()
    assert data["city"] == "Round Rock"
    assert data["street1"] == "1 Main St"


def test_update_unit_not_found(client, seeded):  # pylint: disable=unused-argument
    """Updating an unknown unit is a 404."""
    assert client.put("/units/Texas/Nope", json={"city": "X"}).status_code == 404
