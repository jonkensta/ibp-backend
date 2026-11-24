"""The requests.unit_name migration and its db_bootstrap tier."""

from pathlib import Path

import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import DROP_REQUEST_UNIT_NAME, TOPLEVEL
from .test_package_items_migration import head_revision, scalar

MIGRATION = TOPLEVEL / "alembic" / "versions" / "e9b3d7a1c5f2_add_requests_unit_name.py"
COLUMN = "requests.unit_name"


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
    """Upgrading adds the column, NULL even where the inmate has a unit now;
    downgrading drops it and keeps the request."""
    url = f"sqlite:///{tmp_path / 'unit.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    assert COLUMN not in columns(url)

    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        command.upgrade(config, "head")
        assert COLUMN in columns(url)
        assert scalar(engine, "SELECT unit_name FROM requests") is None

        command.downgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
        assert COLUMN not in columns(url)
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        engine.dispose()


def test_bootstrap_upgrades_database_at_previous_head(tmp_path: Path, monkeypatch):
    """A database at the shipments revision gets the column."""
    url = f"sqlite:///{tmp_path / 'previous.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url), db_bootstrap.PRE_REQUEST_UNIT_REVISION
    )
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        bootstrap(monkeypatch, url)
        assert COLUMN in columns(url)
        assert scalar(engine, "SELECT unit_name FROM requests") is None
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_unit_name_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema with the shipment tables but no requests.unit_name
    gets the column and ends at head."""
    url = f"sqlite:///{tmp_path / 'preunit.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(sqlalchemy.text(DROP_REQUEST_UNIT_NAME))
        insert_filled_request(engine)
        bootstrap(monkeypatch, url)
        assert COLUMN in columns(url)
        assert scalar(engine, "SELECT unit_name FROM requests") is None
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_stamps_current_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema with requests.unit_name ends at head, twice."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        assert COLUMN in columns(url)
        bootstrap(monkeypatch, url)
        bootstrap(monkeypatch, url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()
