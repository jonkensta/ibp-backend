"""Every SQLite connection the app, the shipment sync and the bootstrap make
uses WAL and a busy timeout: the server and a sync's one-off container write
the same file."""

# pylint: disable=import-outside-toplevel

import asyncio
import configparser
import sqlite3
from contextlib import closing
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event

from ibp import db, db_bootstrap


def pragmas(connection) -> tuple[str, int]:
    """The journal mode and busy timeout a connection reports."""
    mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
    timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    return mode, timeout


def file_mode(path: Path) -> str:
    """The journal mode recorded in the file: what a plain connection, with
    no pragmas of its own, gets."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]


def rollback_journal_db(path: Path) -> Path:
    """A database in the default rollback-journal mode, as production's is
    before its first start on this version."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE t (x)")
        connection.commit()
    assert file_mode(path) == "delete"
    return path


def test_sync_engine_uses_wal_and_a_busy_timeout(tmp_path):
    """A sync engine switches an existing database to WAL, which stays
    recorded in the file, and waits for locks."""
    path = rollback_journal_db(tmp_path / "data.db")
    engine = db.build_sync_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert pragmas(connection) == ("wal", db.SQLITE_BUSY_TIMEOUT_MS)
    finally:
        engine.dispose()
    assert file_mode(path) == "wal"
    # The last connection to close folds the WAL back into the file.
    assert not Path(f"{path}-wal").exists()


def test_app_engine_uses_wal_and_a_busy_timeout(tmp_path, monkeypatch):
    """The server's async (aiosqlite) engine sets both on its connections."""
    path = rollback_journal_db(tmp_path / "data.db")
    server_config = configparser.ConfigParser()
    server_config.read_dict({"database": {"uri": f"sqlite+aiosqlite:///{path}"}})
    monkeypatch.setattr(db, "config", server_config)

    async def check():
        engine = db.build_engine()
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(pragmas)
        finally:
            await engine.dispose()

    assert asyncio.run(check()) == ("wal", db.SQLITE_BUSY_TIMEOUT_MS)
    assert file_mode(path) == "wal"


def test_shipment_sync_sessions_use_wal_and_a_busy_timeout(tmp_path, monkeypatch):
    """The sync's sessions, in its own container, set both too."""
    from ibp import shipment_sync

    path = rollback_journal_db(tmp_path / "data.db")
    monkeypatch.setattr(
        db_bootstrap, "get_sync_database_url", lambda: f"sqlite:///{path}"
    )
    with shipment_sync.build_sessions()() as session:
        try:
            assert pragmas(session.connection()) == (
                "wal",
                db.SQLITE_BUSY_TIMEOUT_MS,
            )
        finally:
            session.close()
            session.get_bind().dispose()


def test_alembic_switches_the_database_to_wal(tmp_path):
    """An upgrade run on its own (alembic's env.py engine) switches it."""
    from alembic import command

    path = rollback_journal_db(tmp_path / "data.db")
    url = f"sqlite:///{path}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    assert file_mode(path) == "wal"
    assert "shipments" in db_bootstrap.inspect_database(url)[0]


def test_bootstrap_leaves_one_file_in_wal_mode(tmp_path, monkeypatch):
    """The container's first start on this version: the bootstrap switches
    the database to WAL and, done, leaves no side files beside it."""
    path = tmp_path / "data.db"
    monkeypatch.setattr(
        db_bootstrap, "get_sync_database_url", lambda: f"sqlite:///{path}"
    )
    db_bootstrap.bootstrap()
    assert file_mode(path) == "wal"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.db"]


def test_a_reader_does_not_block_a_writer(tmp_path):
    """The point of WAL: a commit goes through while another connection (a
    backup, a long read) holds a read transaction open."""
    path = rollback_journal_db(tmp_path / "data.db")
    engine = db.build_sync_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as warm:  # switch to WAL first
            warm.exec_driver_sql("SELECT 1")
        with closing(sqlite3.connect(path, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM t").fetchone()
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout = 0")
                connection.exec_driver_sql("INSERT INTO t VALUES (1)")
                connection.commit()
            # The reader keeps its snapshot until it ends.
            assert reader.execute("SELECT count(*) FROM t").fetchone() == (0,)
            reader.execute("COMMIT")
            assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)
    finally:
        engine.dispose()


def test_other_databases_get_no_pragmas():
    """A PostgreSQL engine, sync or async, is left alone (neither connects
    here)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    sync_engine = sa.create_engine("postgresql://ibp@localhost/ibp")
    async_engine = create_async_engine("postgresql+asyncpg://ibp@localhost/ibp")
    db.use_sqlite_pragmas(sync_engine)
    db.use_sqlite_pragmas(async_engine)
    listener = db._set_sqlite_pragmas  # pylint: disable=protected-access
    assert not event.contains(sync_engine, "connect", listener)
    assert not event.contains(async_engine.sync_engine, "connect", listener)
