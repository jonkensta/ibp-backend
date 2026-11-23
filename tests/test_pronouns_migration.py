"""The inmates.pronouns migration and its db_bootstrap tier."""

from pathlib import Path

import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import DROP_SHIPMENT_TABLES
from .test_package_items_migration import head_revision, scalar

PRONOUNS_COLUMNS = {
    "inmates.pronouns",
    "inmates.pronouns_set_by",
    "inmates.pronouns_set_at",
}


def columns(url: str) -> set[str]:
    """Columns (as ``table.column``) of the database at ``url``."""
    _, found = db_bootstrap.inspect_database(url)
    return found


def insert_inmate(engine: sqlalchemy.Engine) -> None:
    """Insert one inmate, with a provider-reported sex, using plain SQL."""
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO inmates (jurisdiction, id, first_name, sex) "
                "VALUES ('Texas', 1, 'JANE', 'Female')"
            )
        )


def test_migration_up_and_down(tmp_path: Path):
    """Upgrading adds empty pronouns columns; downgrading drops them."""
    url = f"sqlite:///{tmp_path / 'pronouns.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_PRONOUNS_REVISION)
    assert not PRONOUNS_COLUMNS & columns(url)

    engine = sqlalchemy.create_engine(url)
    try:
        insert_inmate(engine)
        command.upgrade(config, "head")
        assert PRONOUNS_COLUMNS <= columns(url)
        # Nothing is backfilled, whatever the provider says about sex.
        assert (
            scalar(
                engine,
                "SELECT COUNT(*) FROM inmates WHERE pronouns IS NULL "
                "AND pronouns_set_by IS NULL AND pronouns_set_at IS NULL",
            )
            == 1
        )

        command.downgrade(config, db_bootstrap.PRE_PRONOUNS_REVISION)
        assert not PRONOUNS_COLUMNS & columns(url)
        assert scalar(engine, "SELECT first_name FROM inmates") == "JANE"
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_pronouns_schema(tmp_path: Path, monkeypatch):
    """A create_all schema from before pronouns gets the migration."""
    url = f"sqlite:///{tmp_path / 'prepronouns.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for statement in DROP_SHIPMENT_TABLES:
                connection.execute(sqlalchemy.text(statement))
            for column in sorted(PRONOUNS_COLUMNS):
                table, name = column.split(".")
                connection.execute(
                    sqlalchemy.text(f"ALTER TABLE {table} DROP COLUMN {name}")  # nosec
                )
        insert_inmate(engine)

        monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
        db_bootstrap.bootstrap()

        assert PRONOUNS_COLUMNS <= columns(url)
        assert scalar(engine, "SELECT pronouns FROM inmates") is None
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_stamps_schema_with_pronouns(tmp_path: Path, monkeypatch):
    """A create_all schema that has inmates.pronouns ends up at head."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        assert PRONOUNS_COLUMNS <= columns(url)
        monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
        db_bootstrap.bootstrap()
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()
