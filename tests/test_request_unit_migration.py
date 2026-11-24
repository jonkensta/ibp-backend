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


def test_downgrade_with_foreign_keys_on_keeps_links(tmp_path: Path):
    """Downgrading doesn't rebuild requests, so with foreign keys on its
    cascade doesn't empty package_shipments."""
    url = f"sqlite:///{tmp_path / 'links.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, "head")
    engine = sqlalchemy.create_engine(url)

    def foreign_keys_on(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    sqlalchemy.event.listen(sqlalchemy.engine.Engine, "connect", foreign_keys_on)
    try:
        insert_filled_request(engine)
        insert_shipment(engine)
        assert scalar(engine, "PRAGMA foreign_keys") == 1
        command.downgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
        assert not COLUMNS & columns(url)
        assert scalar(engine, "SELECT COUNT(*) FROM package_shipments") == 1
        assert scalar(engine, "SELECT COUNT(*) FROM requests") == 1
    finally:
        sqlalchemy.event.remove(sqlalchemy.engine.Engine, "connect", foreign_keys_on)
        engine.dispose()


def interrupted_downgrade(tmp_path: Path) -> str:
    """A database at head whose downgrade dropped unit_recorded and stopped
    before dropping unit_name or recording the revision."""
    url = f"sqlite:///{tmp_path / 'halfdown.db'}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("ALTER TABLE requests DROP COLUMN unit_recorded")
            )
    finally:
        engine.dispose()
    return url


def test_bootstrap_restores_a_column_an_interrupted_downgrade_dropped(
    tmp_path: Path, monkeypatch
):
    """At head with unit_recorded missing, the next start restores it
    instead of reporting the schema as fine."""
    url = interrupted_downgrade(tmp_path)
    bootstrap(monkeypatch, url)
    assert COLUMNS <= columns(url)
    engine = sqlalchemy.create_engine(url)
    try:
        assert rows(engine, RECORDED) == [(None, 0)]
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_interrupted_downgrade_can_be_run_again(tmp_path: Path):
    """A downgrade run again after it dropped one column drops the other."""
    url = interrupted_downgrade(tmp_path)
    command.downgrade(
        db_bootstrap.build_alembic_config(url), db_bootstrap.PRE_REQUEST_UNIT_REVISION
    )
    assert not COLUMNS & columns(url)


def test_bootstrap_refuses_a_head_schema_missing_another_column(
    tmp_path: Path, monkeypatch
):
    """A column missing at head that no revision can restore fails the
    bootstrap loudly."""
    url = f"sqlite:///{tmp_path / 'broken.db'}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("ALTER TABLE inmates DROP COLUMN pronouns_set_by")
            )
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="lacks columns: inmates.pronouns_set_by"):
        bootstrap(monkeypatch, url)


def interrupt_on(statement_start: str):
    """A listener failing the first statement starting with
    ``statement_start``, as if the process died there."""

    def fail(conn, cursor, statement, *args):  # pylint: disable=unused-argument
        if statement.startswith(statement_start):
            raise KeyboardInterrupt("interrupted")

    return fail


def test_interrupted_downgrade_changes_nothing(tmp_path: Path):
    """A downgrade stopped between dropping its two columns leaves both
    columns and the revision as they were: one atomic step."""
    url = f"sqlite:///{tmp_path / 'atomic.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, "head")
    engine = sqlalchemy.create_engine(url)
    listener = interrupt_on("ALTER TABLE requests DROP COLUMN unit_name")
    sqlalchemy.event.listen(sqlalchemy.engine.Engine, "before_cursor_execute", listener)
    try:
        insert_filled_request(engine)
        with pytest.raises(KeyboardInterrupt):
            command.downgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    finally:
        sqlalchemy.event.remove(
            sqlalchemy.engine.Engine, "before_cursor_execute", listener
        )
    try:
        assert COLUMNS <= columns(url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_interrupted_upgrade_changes_nothing(tmp_path: Path):
    """An upgrade stopped between adding its two columns leaves neither."""
    url = f"sqlite:///{tmp_path / 'atomicup.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_REQUEST_UNIT_REVISION)
    listener = interrupt_on("ALTER TABLE requests ADD COLUMN unit_recorded")
    sqlalchemy.event.listen(sqlalchemy.engine.Engine, "before_cursor_execute", listener)
    try:
        with pytest.raises(KeyboardInterrupt):
            command.upgrade(config, "head")
    finally:
        sqlalchemy.event.remove(
            sqlalchemy.engine.Engine, "before_cursor_execute", listener
        )
    assert not COLUMNS & columns(url)
    engine = sqlalchemy.create_engine(url)
    try:
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            db_bootstrap.PRE_REQUEST_UNIT_REVISION
        )
    finally:
        engine.dispose()


def half_state(tmp_path: Path, recorded_unit, drop: str) -> str:
    """A database at head with one request that recorded ``recorded_unit``
    (None: an unknown unit), then ``drop`` dropped as a half-run downgrade
    once could."""
    url = f"sqlite:///{tmp_path / 'half.db'}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text(
                    "UPDATE requests SET unit_name = :unit, unit_recorded = 1"
                ),
                {"unit": recorded_unit},
            )
            connection.execute(
                sqlalchemy.text(f"ALTER TABLE requests DROP COLUMN {drop}")  # nosec
            )
    finally:
        engine.dispose()
    return url


def test_restored_unit_recorded_keeps_the_recorded_units(
    tmp_path: Path, monkeypatch, capsys
):
    """With unit_recorded lost and unit_name kept, a request that stored a
    unit is restored as recorded (so it keeps linking to that unit, not the
    inmate's current one), and the unrecoverable rest is warned about."""
    url = half_state(tmp_path, "ALPHA", "unit_recorded")
    bootstrap(monkeypatch, url)
    engine = sqlalchemy.create_engine(url)
    try:
        assert rows(engine, RECORDED) == [("ALPHA", 1)]
    finally:
        engine.dispose()
    # alembic's logging (alembic.ini) writes to stderr.
    warning = capsys.readouterr().err
    assert "requests filled while their inmate's unit was unknown" in warning


def test_restored_unit_recorded_cannot_recover_an_unknown_unit(
    tmp_path: Path, monkeypatch
):
    """A request that recorded an unknown unit can't be told from an older
    one once unit_recorded is gone: it is restored as not recorded."""
    url = half_state(tmp_path, None, "unit_recorded")
    bootstrap(monkeypatch, url)
    engine = sqlalchemy.create_engine(url)
    try:
        assert rows(engine, RECORDED) == [(None, 0)]
    finally:
        engine.dispose()


def test_lost_unit_names_refuse_startup(tmp_path: Path, monkeypatch):
    """With unit_name lost while requests recorded units, startup fails
    rather than link them by the inmate's current unit."""
    url = half_state(tmp_path, "ALPHA", "unit_name")
    with pytest.raises(RuntimeError, match="1 requests recorded a unit at fill"):
        bootstrap(monkeypatch, url)
    assert "requests.unit_name" not in columns(url)


def test_lost_unit_name_with_nothing_recorded_is_restored(tmp_path: Path, monkeypatch):
    """With unit_name lost but no request having recorded a unit, nothing is
    lost: the column is restored."""
    url = f"sqlite:///{tmp_path / 'nothing.db'}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    engine = sqlalchemy.create_engine(url)
    try:
        insert_filled_request(engine)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("ALTER TABLE requests DROP COLUMN unit_name")
            )
    finally:
        engine.dispose()
    bootstrap(monkeypatch, url)
    assert COLUMNS <= columns(url)
