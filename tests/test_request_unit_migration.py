"""The requests.unit_name and unit_recorded migration and its db_bootstrap tier."""

from pathlib import Path

import pytest
import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import DROP_REQUEST_UNIT_COLUMNS, TOPLEVEL
from .test_package_items_migration import head_revision, scalar
from .test_shipments_migration import insert_shipment

MIGRATION = (
    TOPLEVEL / "alembic" / "versions" / "e9b3d7a1c5f2_add_requests_unit_at_fill.py"
)
COLUMNS = {"requests.unit_name", "requests.unit_recorded"}
RECORDED = "SELECT unit_name, unit_recorded FROM requests"


def columns(url: str) -> set[str]:
    """Columns (as ``table.column``) of the database at ``url``."""
    _, found = db_bootstrap.inspect_database(url)
    return found


def insert_filled_request(engine: sqlalchemy.Engine) -> None:
    """Insert an inmate at an invented unit with one filled request."""
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO units (jurisdiction, name, street1, city, zipcode, "
                "state) VALUES ('Texas', 'ALPHA', '1 Example Rd', 'Exampleton', "
                "'77000', 'TX')"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO inmates (jurisdiction, id, unit_name) "
                "VALUES ('Texas', 1, 'ALPHA')"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO requests (inmate_jurisdiction, inmate_id, "
                '"index", date_processed, date_postmarked, action) '
                "VALUES ('Texas', 1, 0, '2026-01-02', '2026-01-01', 'Filled')"
            )
        )


def rows(engine: sqlalchemy.Engine, sql: str) -> list[tuple]:
    """Run a query and return its rows."""
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(sqlalchemy.text(sql))]


def bootstrap(monkeypatch, url: str) -> None:
    """Run db_bootstrap against ``url``."""
    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()


def test_migration_is_the_only_head_after_shipments():
    """The revision is the single head, right after the shipments one."""
    assert head_revision() == "e9b3d7a1c5f2"
    source = MIGRATION.read_text(encoding="utf-8")
    assert "down_revision: Union[str, None] = 'a3c8e6f2d9b4'" in source
    assert db_bootstrap.PRE_REQUEST_UNIT_REVISION == "a3c8e6f2d9b4"


def test_migration_up_and_down_backfills_nothing(tmp_path: Path):
    """Upgrading adds the columns, recording nothing even where the inmate
    has a unit now; downgrading drops them and keeps the request."""
    url = f"sqlite:///{tmp_path / 'unit.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    assert not COLUMNS & columns(url)

    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        command.upgrade(config, "head")
        assert COLUMNS <= columns(url)
        assert rows(engine, RECORDED) == [(None, 0)]

        command.downgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
        assert not COLUMNS & columns(url)
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        engine.dispose()


def test_bootstrap_upgrades_database_at_previous_head(tmp_path: Path, monkeypatch):
    """A database at the shipments revision gets the columns."""
    url = f"sqlite:///{tmp_path / 'previous.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url), db_bootstrap.PRE_REQUEST_UNIT_REVISION
    )
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        bootstrap(monkeypatch, url)
        assert COLUMNS <= columns(url)
        assert rows(engine, RECORDED) == [(None, 0)]
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_unit_name_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema with the shipment tables but no unit columns gets
    them and ends at head."""
    url = f"sqlite:///{tmp_path / 'preunit.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for statement in DROP_REQUEST_UNIT_COLUMNS:
                connection.execute(sqlalchemy.text(statement))
        insert_filled_request(engine)
        bootstrap(monkeypatch, url)
        assert COLUMNS <= columns(url)
        assert rows(engine, RECORDED) == [(None, 0)]
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_stamps_current_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema with the unit columns ends at head, twice."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        assert COLUMNS <= columns(url)
        bootstrap(monkeypatch, url)
        bootstrap(monkeypatch, url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "already",
    [
        ["unit_name VARCHAR"],
        ["unit_name VARCHAR", "unit_recorded BOOLEAN DEFAULT 0 NOT NULL"],
    ],
)
def test_interrupted_upgrade_can_be_run_again(tmp_path: Path, already):
    """An upgrade that added columns but never recorded the revision (SQLite
    DDL isn't transactional) succeeds when run again."""
    url = f"sqlite:///{tmp_path / 'interrupted.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        with engine.begin() as connection:
            for column in already:
                connection.execute(
                    sqlalchemy.text(f"ALTER TABLE requests ADD COLUMN {column}")
                )
        command.upgrade(config, "head")
        assert COLUMNS <= columns(url)
        assert rows(engine, RECORDED) == [(None, 0)]
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("column", ["unit_recorded VARCHAR", "unit_recorded BOOLEAN"])
def test_upgrade_refuses_a_different_existing_column(tmp_path: Path, column):
    """A column already there with another type or nullability fails the
    upgrade instead of being taken as this revision's."""
    url = f"sqlite:///{tmp_path / 'different.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text(f"ALTER TABLE requests ADD COLUMN {column}")
            )
        with pytest.raises(RuntimeError, match="requests.unit_recorded exists"):
            command.upgrade(config, "head")
    finally:
        engine.dispose()
