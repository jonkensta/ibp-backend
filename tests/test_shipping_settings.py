"""Tests for the editable shipping settings."""

# pylint: disable=redefined-outer-name

import copy

import pytest
import sqlalchemy

from alembic import command

from ibp import db_bootstrap, models

DEFAULTS = {
    "return_address": {
        "name": "INSIDE BOOKS PROJECT",
        "street1": "PO BOX 301029",
        "street2": "12 ST BOOKS",
        "city": "AUSTIN",
        "state": "TX",
        "zipcode": "78703",
    },
    "unit_recipient_name": "ATTN: MAILROOM STAFF",
}

UPDATED = {
    "return_address": {
        "name": "Books Org",
        "street1": "1 Elm St",
        "street2": None,
        "city": "Dallas",
        "state": "TX",
        "zipcode": "75201-1234",
    },
    "unit_recipient_name": "ATTN: MAIL ROOM",
}


def with_address(**changes):
    """Return UPDATED with return address fields replaced."""
    body = copy.deepcopy(UPDATED)
    body["return_address"].update(changes)
    return body


def test_get_defaults_on_fresh_db(client):
    """A schema without the seeded row still returns the production defaults."""
    response = client.get("/shipping-settings")
    assert response.status_code == 200
    assert response.json() == DEFAULTS


def test_put_roundtrip(client, api_db):
    """A PUT is saved, returned, and read back by GET."""
    response = client.put("/shipping-settings", json=UPDATED)
    assert response.status_code == 200
    assert response.json() == UPDATED
    assert client.get("/shipping-settings").json() == UPDATED

    # A second PUT updates the single row in place.
    response = client.put("/shipping-settings", json=DEFAULTS)
    assert response.status_code == 200
    assert client.get("/shipping-settings").json() == DEFAULTS
    with api_db() as session:
        assert session.query(models.ShippingSettings).count() == 1


def test_put_normalises(client):
    """Whitespace is stripped, blank street2 is null, state is upper-cased."""
    body = with_address(name="  Books Org  ", street2="   ", state=" tx ")
    body["unit_recipient_name"] = " ATTN: MAIL ROOM "
    response = client.put("/shipping-settings", json=body)
    assert response.status_code == 200
    assert response.json() == UPDATED


def test_put_keeps_street2(client):
    """A non-blank street2 is kept."""
    response = client.put("/shipping-settings", json=with_address(street2="Ste 5"))
    assert response.status_code == 200
    assert response.json()["return_address"]["street2"] == "Ste 5"


def test_put_street2_may_be_omitted(client):
    """street2 is optional."""
    body = copy.deepcopy(UPDATED)
    del body["return_address"]["street2"]
    response = client.put("/shipping-settings", json=body)
    assert response.status_code == 200
    assert response.json()["return_address"]["street2"] is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", ""),
        ("name", "   "),
        ("name", None),
        ("name", "x" * 101),
        ("street1", ""),
        ("street1", "   "),
        ("street2", "x" * 101),
        ("city", ""),
        ("city", "  "),
        ("state", "T"),
        ("state", "TEX"),
        ("state", "Texas"),
        ("state", "T1"),
        ("state", ""),
        ("zipcode", "7870"),
        ("zipcode", "787033"),
        ("zipcode", "78703-12"),
        ("zipcode", "ABCDE"),
        ("zipcode", ""),
    ],
)
def test_put_rejects_invalid_address(client, field, value):
    """Invalid return address fields are a 422 naming the field."""
    response = client.put("/shipping-settings", json=with_address(**{field: value}))
    assert response.status_code == 422
    locations = [error["loc"] for error in response.json()["detail"]]
    assert any(loc[:3] == ["body", "return_address", field] for loc in locations)
    assert client.get("/shipping-settings").json() == DEFAULTS


@pytest.mark.parametrize("field", ["name", "street1", "city", "state", "zipcode"])
def test_put_rejects_missing_address_field(client, field):
    """Required return address fields cannot be omitted."""
    body = copy.deepcopy(UPDATED)
    del body["return_address"][field]
    response = client.put("/shipping-settings", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("value", ["", "   ", None, "x" * 101])
def test_put_rejects_invalid_unit_recipient_name(client, value):
    """unit_recipient_name is required and non-blank."""
    body = copy.deepcopy(UPDATED)
    body["unit_recipient_name"] = value
    response = client.put("/shipping-settings", json=body)
    assert response.status_code == 422
    locations = [error["loc"] for error in response.json()["detail"]]
    assert ["body", "unit_recipient_name"] in [loc[:2] for loc in locations]


def test_put_rejects_missing_sections(client):
    """Both top-level keys are required."""
    assert client.put("/shipping-settings", json={}).status_code == 422
    body = {"return_address": UPDATED["return_address"]}
    assert client.put("/shipping-settings", json=body).status_code == 422


def read_settings_rows(url: str) -> list[dict]:
    """Read every shipping_settings row from the database at ``url``."""
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sqlalchemy.text("SELECT * FROM shipping_settings")
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


SEEDED_ROW = {"id": 1, **models.DEFAULT_SHIPPING_SETTINGS}


def test_migration_from_previous_head_seeds_row(tmp_path):
    """Upgrading from a7c31f9e5b24 creates and seeds the settings row."""
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, "a7c31f9e5b24")
    command.upgrade(config, "head")
    assert read_settings_rows(url) == [SEEDED_ROW]

    command.downgrade(config, "a7c31f9e5b24")
    engine = sqlalchemy.create_engine(url)
    try:
        assert "shipping_settings" not in sqlalchemy.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_bootstrap_seeds_create_all_schema(tmp_path, monkeypatch):
    """A create_all schema (scripts/migrate.py) is stamped and seeded."""
    url = f"sqlite:///{tmp_path / 'created.db'}"
    engine = sqlalchemy.create_engine(url)
    models.Base.metadata.create_all(engine)
    engine.dispose()

    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()
    assert read_settings_rows(url) == [SEEDED_ROW]

    # Idempotent: a second run neither fails nor duplicates the row.
    db_bootstrap.bootstrap()
    assert read_settings_rows(url) == [SEEDED_ROW]


def test_bootstrap_adopts_pre_settings_create_all_schema(tmp_path, monkeypatch):
    """A create_all schema from before shipping_settings gets the migration."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = sqlalchemy.create_engine(url)
    tables = [
        table
        for name, table in models.Base.metadata.tables.items()
        if name != "shipping_settings"
    ]
    models.Base.metadata.create_all(engine, tables=tables)
    engine.dispose()

    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()
    assert read_settings_rows(url) == [SEEDED_ROW]


def test_bootstrap_fresh_db_seeds_row(tmp_path, monkeypatch):
    """An empty database is built by the migrations, which seed the row."""
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()
    assert read_settings_rows(url) == [SEEDED_ROW]
