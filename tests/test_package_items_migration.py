"""The package_items migration, its bootstrap tier and the legacy migration."""

from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

import ibp.models
from ibp import db_bootstrap

from .conftest import TOPLEVEL
from .test_migrate_end_to_end import run_migrate

PACKAGE_TABLES = {"package_items", "registrations"}
# Tables added after the package tables, absent from a phase 1 schema too.
LATER_TABLES = {"upcs"}


def head_revision() -> str:
    """The alembic head revision."""
    config = Config(str(TOPLEVEL / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def insert_request(engine: sqlalchemy.Engine) -> None:
    """Insert an inmate with one filled request using plain SQL."""
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO inmates (jurisdiction, id) VALUES ('Texas', 1)"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO requests (inmate_jurisdiction, inmate_id, "
                '"index", date_processed, date_postmarked, action) '
                "VALUES ('Texas', 1, 0, '2026-01-02', '2026-01-01', 'Filled')"
            )
        )


def scalar(engine: sqlalchemy.Engine, sql: str):
    """Run a query and return its single value."""
    with engine.connect() as connection:
        return connection.execute(sqlalchemy.text(sql)).scalar_one()


def test_migration_upgrade_and_downgrade(tmp_path: Path):
    """Upgrading keeps requests (revision 0); downgrading drops the new schema."""
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_PACKAGE_ITEMS_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_request(engine)
        command.upgrade(config, "head")
        assert scalar(engine, "SELECT contents_revision FROM requests") == 0
        assert PACKAGE_TABLES <= set(sqlalchemy.inspect(engine).get_table_names())

        with engine.begin() as connection:
            for _ in range(2):
                connection.execute(
                    sqlalchemy.text(
                        "INSERT INTO package_items (inmate_jurisdiction, inmate_id, "
                        "request_index, title, source, created_at, updated_at) "
                        "VALUES ('Texas', 1, 0, 'A Book', 'manual', "
                        "'2026-01-03', '2026-01-03')"
                    )
                )
            connection.execute(
                sqlalchemy.text("DELETE FROM package_items WHERE id = 2")
            )
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO package_items (inmate_jurisdiction, inmate_id, "
                    "request_index, title, source, created_at, updated_at) "
                    "VALUES ('Texas', 1, 0, 'B Book', 'manual', "
                    "'2026-01-03', '2026-01-03')"
                )
            )
        # AUTOINCREMENT in the migrated table: the deleted id 2 isn't reused.
        assert scalar(engine, "SELECT MAX(id) FROM package_items") == 3
        assert scalar(engine, "SELECT revision FROM package_items WHERE id = 3") == 0

        command.downgrade(config, db_bootstrap.PRE_PACKAGE_ITEMS_REVISION)
        inspector = sqlalchemy.inspect(engine)
        assert not PACKAGE_TABLES & set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("requests")}
        assert "contents_revision" not in columns
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_package_items_schema(tmp_path: Path, monkeypatch):
    """A create_all schema from phase 1 (no package_items) gets the migration."""
    url = f"sqlite:///{tmp_path / 'phase1.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        tables = [
            table
            for name, table in ibp.models.Base.metadata.tables.items()
            if name not in PACKAGE_TABLES | LATER_TABLES
        ]
        ibp.models.Base.metadata.create_all(engine, tables=tables)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("ALTER TABLE requests DROP COLUMN contents_revision")
            )
        insert_request(engine)

        monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
        db_bootstrap.bootstrap()

        assert PACKAGE_TABLES <= set(sqlalchemy.inspect(engine).get_table_names())
        assert scalar(engine, "SELECT contents_revision FROM requests") == 0
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_stamps_current_schema(tmp_path: Path, monkeypatch):
    """A create_all schema from the current models is stamped head as is."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
        db_bootstrap.bootstrap()
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_legacy_migration_has_package_tables(legacy_db: Path, tmp_path: Path):
    """scripts/migrate.py builds the package tables and stamps head."""
    destination = tmp_path / "new.db"
    result = run_migrate(legacy_db, destination, "--archive-dir", str(tmp_path / "a"))
    assert result.returncode == 0, result.stdout + result.stderr
    engine = sqlalchemy.create_engine(f"sqlite:///{destination}")
    try:
        assert PACKAGE_TABLES <= set(sqlalchemy.inspect(engine).get_table_names())
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
        assert scalar(engine, "SELECT SUM(contents_revision) FROM requests") == 0
    finally:
        engine.dispose()
