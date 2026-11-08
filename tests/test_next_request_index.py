"""The inmates.next_request_index migration, bootstrap and legacy migration."""

import datetime

import sqlalchemy
from sqlalchemy.orm import Session

from alembic import command

import ibp.models
from ibp import db_bootstrap

# (jurisdiction, inmate id, request indexes): one with a gap left by a
# deletion, one Federal inmate sharing the Texas inmate's id, one with none.
INMATES = [
    ("Texas", 1001, [0, 1, 4]),
    ("Federal", 1001, [0]),
    ("Texas", 2002, []),
]
EXPECTED = {("Texas", 1001): 5, ("Federal", 1001): 1, ("Texas", 2002): 0}


def insert_inmates(engine: sqlalchemy.Engine) -> None:
    """Insert INMATES and their requests with plain SQL (any schema era)."""
    with engine.begin() as connection:
        for jurisdiction, inmate_id, indexes in INMATES:
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO inmates (jurisdiction, id) VALUES (:j, :i)"
                ),
                {"j": jurisdiction, "i": inmate_id},
            )
            for index in indexes:
                connection.execute(
                    sqlalchemy.text(
                        "INSERT INTO requests (inmate_jurisdiction, inmate_id, "
                        '"index", date_processed, date_postmarked, action) '
                        "VALUES (:j, :i, :x, '2026-01-02', '2026-01-01', 'Filled')"
                    ),
                    {"j": jurisdiction, "i": inmate_id, "x": index},
                )


def next_request_indexes(engine: sqlalchemy.Engine) -> dict[tuple[str, int], int]:
    """Map each inmate's (jurisdiction, id) to its next_request_index."""
    with engine.connect() as connection:
        rows = connection.execute(
            sqlalchemy.text("SELECT jurisdiction, id, next_request_index FROM inmates")
        )
        return {(row[0], row[1]): row[2] for row in rows}


def inmate_columns(engine: sqlalchemy.Engine) -> set[str]:
    """Column names of the inmates table."""
    return {
        column["name"] for column in sqlalchemy.inspect(engine).get_columns("inmates")
    }


def test_migration_sets_next_request_index(tmp_path):
    """Upgrading sets max(index) + 1 per inmate (0 with no requests)."""
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_NEXT_REQUEST_INDEX_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_inmates(engine)
        command.upgrade(config, "head")
        assert next_request_indexes(engine) == EXPECTED

        command.downgrade(config, db_bootstrap.PRE_NEXT_REQUEST_INDEX_REVISION)
        assert "next_request_index" not in inmate_columns(engine)
        with engine.connect() as connection:
            for table, expected in [("inmates", 3), ("requests", 4)]:
                count = connection.execute(
                    sqlalchemy.text(f"SELECT COUNT(*) FROM {table}")  # nosec
                ).scalar()
                assert count == expected, table
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_next_request_index_schema(tmp_path, monkeypatch):
    """A create_all schema with books but no counter replays the migration."""
    url = f"sqlite:///{tmp_path / 'prephase1.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for statement in [
                "DROP TABLE upcs",
                "DROP TABLE registrations",
                "DROP TABLE package_items",
                "ALTER TABLE requests DROP COLUMN contents_revision",
                "ALTER TABLE inmates DROP COLUMN next_request_index",
            ]:
                connection.execute(sqlalchemy.text(statement))
        insert_inmates(engine)

        monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
        db_bootstrap.bootstrap()

        assert next_request_indexes(engine) == EXPECTED
        with engine.connect() as connection:
            version = connection.execute(
                sqlalchemy.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version != db_bootstrap.PRE_NEXT_REQUEST_INDEX_REVISION
    finally:
        engine.dispose()


def test_bootstrap_reports_columns(tmp_path):
    """inspect_database reports the head marker column of a current schema."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    ibp.models.Base.metadata.create_all(engine)
    engine.dispose()
    tables, columns = db_bootstrap.inspect_database(url)
    assert "books" in tables
    assert {"inmates.next_request_index", "requests.request_id"} <= columns


def test_new_inmate_starts_at_zero(tmp_path):
    """An inmate inserted through the ORM (e.g. a provider upsert) starts at 0."""
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    ibp.models.Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                ibp.models.Inmate(
                    jurisdiction="Texas",
                    id=3003,
                    datetime_fetched=datetime.datetime.now(datetime.timezone.utc),
                )
            )
        assert next_request_indexes(engine) == {("Texas", 3003): 0}
    finally:
        engine.dispose()
