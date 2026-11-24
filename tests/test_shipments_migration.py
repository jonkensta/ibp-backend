"""The shipment tracking migration and its db_bootstrap tier."""

from pathlib import Path

import pytest
import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import (
    DROP_REQUEST_UNIT_NAME,
    DROP_SHIPMENT_TABLES,
    SHIPMENT_TABLES,
    TOPLEVEL,
)
from .test_package_items_migration import head_revision, insert_request, scalar
from .test_race_sex_migration import ROWS, after, insert_rows, stored

MIGRATION = TOPLEVEL / "alembic" / "versions" / "a3c8e6f2d9b4_add_shipment_tracking.py"


def table_names(url: str) -> set[str]:
    """Tables of the database at ``url``."""
    tables, _ = db_bootstrap.inspect_database(url)
    return tables


def bootstrap(monkeypatch, url: str) -> None:
    """Run db_bootstrap against ``url``."""
    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()


def insert_shipment(engine: sqlalchemy.Engine) -> None:
    """Insert one label with an event and a link to request (Texas, 1, 0)."""
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO shipments (id, easypost_id, tracking_code, "
                "purchased_at, kind, synced_at) VALUES (1, 'shp_1', '9400', "
                "'2026-01-03 15:00:00', 'individual', '2026-01-04 12:00:00')"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO tracking_events (shipment_id, occurred_at, status) "
                "VALUES (1, '2026-01-05 10:00:00', 'delivered')"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO package_shipments (inmate_jurisdiction, inmate_id, "
                "request_index, shipment_id, basis) "
                "VALUES ('Texas', 1, 0, 1, 'inmate_number')"
            )
        )


def test_migration_follows_race_sex():
    """The revision comes right after the race and sex one."""
    source = MIGRATION.read_text(encoding="utf-8")
    assert "down_revision: Union[str, None] = 'd2a6f8c3e1b7'" in source
    assert db_bootstrap.PRE_SHIPMENTS_REVISION == "d2a6f8c3e1b7"


def test_migration_up_and_down(tmp_path: Path):
    """Upgrading adds the three empty tables; downgrading drops only them."""
    url = f"sqlite:///{tmp_path / 'shipments.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_SHIPMENTS_REVISION)
    assert not set(SHIPMENT_TABLES) & table_names(url)

    engine = sqlalchemy.create_engine(url)
    try:
        insert_request(engine)
        command.upgrade(config, "head")
        assert set(SHIPMENT_TABLES) <= table_names(url)
        assert scalar(engine, "SELECT COUNT(*) FROM shipments") == 0
        insert_shipment(engine)

        command.downgrade(config, db_bootstrap.PRE_SHIPMENTS_REVISION)
        assert not set(SHIPMENT_TABLES) & table_names(url)
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("schema", ["migrated", "create_all"])
def test_event_uniqueness_treats_missing_values_alike(tmp_path: Path, schema: str):
    """The same event can't be stored twice, even with no place given; a
    repeat needs the next occurrence."""
    url = f"sqlite:///{tmp_path / 'events.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        if schema == "migrated":
            command.upgrade(db_bootstrap.build_alembic_config(url), "head")
        else:
            ibp.models.Base.metadata.create_all(engine)
        insert_request(engine)
        insert_shipment(engine)
        duplicate = sqlalchemy.text(
            "INSERT INTO tracking_events (shipment_id, occurred_at, status, "
            "occurrence) VALUES (1, '2026-01-05 10:00:00', 'delivered', :n)"
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with engine.begin() as connection:
                connection.execute(duplicate, {"n": 0})
        with engine.begin() as connection:
            connection.execute(duplicate, {"n": 1})
        assert scalar(engine, "SELECT COUNT(*) FROM tracking_events") == 2
    finally:
        engine.dispose()


def test_bootstrap_upgrades_database_at_previous_head(tmp_path: Path, monkeypatch):
    """A database at the race and sex revision gets the new tables."""
    url = f"sqlite:///{tmp_path / 'previous.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url), db_bootstrap.PRE_SHIPMENTS_REVISION
    )
    engine = sqlalchemy.create_engine(url)
    try:
        insert_request(engine)
        bootstrap(monkeypatch, url)
        assert set(SHIPMENT_TABLES) <= table_names(url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        engine.dispose()


def test_upgrade_from_before_race_sex_applies_it_on_the_way(tmp_path: Path):
    """Upgrading a pronouns-era database to head runs the data migration too."""
    url = f"sqlite:///{tmp_path / 'pronouns.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_RACE_SEX_WORDS_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_rows(engine, ROWS)
        command.upgrade(config, "head")
        assert stored(engine) == after(ROWS)
        assert set(SHIPMENT_TABLES) <= table_names(url)
    finally:
        engine.dispose()


def test_bootstrap_adopts_current_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema with the shipment tables still gets the race and
    sex rewrite, ends at head, and a second bootstrap changes nothing."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        insert_rows(engine, ROWS)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(ROWS)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(ROWS)
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_shipments_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema from before this change gets the rewrite and the
    new tables."""
    url = f"sqlite:///{tmp_path / 'preshipments.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for statement in [*DROP_SHIPMENT_TABLES, DROP_REQUEST_UNIT_NAME]:
                connection.execute(sqlalchemy.text(statement))
        insert_rows(engine, ROWS)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(ROWS)
        assert set(SHIPMENT_TABLES) <= table_names(url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()
