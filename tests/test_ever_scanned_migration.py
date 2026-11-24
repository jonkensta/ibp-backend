"""The shipments.ever_scanned migration and its db_bootstrap tiers."""

from pathlib import Path

import pytest
import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import DROP_EVER_SCANNED, TOPLEVEL
from .test_package_items_migration import head_revision, insert_request, scalar

MIGRATION = (
    TOPLEVEL / "alembic" / "versions" / "f4c2a8e6b1d3_add_shipments_ever_scanned.py"
)
COLUMN = "shipments.ever_scanned"


def columns(url: str) -> set[str]:
    """Columns (as ``table.column``) of the database at ``url``."""
    _, found = db_bootstrap.inspect_database(url)
    return found


def bootstrap(monkeypatch, url: str) -> None:
    """Run db_bootstrap against ``url``."""
    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()


# (id, tracker status, event statuses, scanned): invented labels.
LABELS = [
    (1, "in_transit", [], True),  # its status alone
    (2, "pre_transit", ["pre_transit", "in_transit"], True),  # an event alone
    (3, "pre_transit", ["pre_transit"], False),
    (4, None, [], False),
    (5, "error", ["unknown", None], False),
    (6, "delivered", ["delivered"], True),
]


def insert_labels(engine: sqlalchemy.Engine) -> None:
    """Insert LABELS with their events, using plain SQL."""
    with engine.begin() as connection:
        for label_id, status, events, _ in LABELS:
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO shipments (id, easypost_id, tracking_code, "
                    "purchased_at, kind, tracker_status, synced_at) VALUES "
                    "(:id, :ep, '9400', '2026-01-03 15:00:00', 'bulk', :status, "
                    "'2026-01-04 12:00:00')"
                ),
                {"id": label_id, "ep": f"shp_{label_id:08d}", "status": status},
            )
            for n, event in enumerate(events):
                connection.execute(
                    sqlalchemy.text(
                        "INSERT INTO tracking_events (shipment_id, occurred_at, "
                        "status, occurrence) VALUES (:id, :at, :status, 0)"
                    ),
                    {
                        "id": label_id,
                        "at": f"2026-01-0{n + 4} 10:00:00",
                        "status": event,
                    },
                )


def flags(engine: sqlalchemy.Engine) -> dict[int, bool]:
    """Each label's ever_scanned."""
    with engine.connect() as connection:
        return {
            row[0]: bool(row[1])
            for row in connection.execute(
                sqlalchemy.text("SELECT id, ever_scanned FROM shipments")
            )
        }


EXPECTED = {label_id: scanned for label_id, _, _, scanned in LABELS}
EVENTS = sum(len(events) for _, _, events, _ in LABELS)


def test_migration_is_the_only_head_after_request_units():
    """The revision is the single head, right after the request unit one."""
    assert head_revision() == "f4c2a8e6b1d3"
    source = MIGRATION.read_text(encoding="utf-8")
    assert "down_revision: Union[str, None] = 'e9b3d7a1c5f2'" in source
    assert db_bootstrap.PRE_EVER_SCANNED_REVISION == "e9b3d7a1c5f2"


def test_upgrade_backfills_from_status_and_events_and_downgrade_drops(tmp_path: Path):
    """Upgrading sets the flag for labels whose status or any event shows a
    scan; downgrading drops it, keeping labels and events."""
    url = f"sqlite:///{tmp_path / 'scanned.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_labels(engine)
        command.upgrade(config, "head")
        assert flags(engine) == EXPECTED
        command.downgrade(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)
        assert COLUMN not in columns(url)
        assert scalar(engine, "SELECT COUNT(*) FROM tracking_events") == EVENTS
        command.downgrade(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)  # no-op
    finally:
        engine.dispose()


def test_upgrade_is_idempotent_and_never_clears(tmp_path: Path):
    """Run again over an existing column, the upgrade adds nothing and
    leaves a flag already set alone."""
    url = f"sqlite:///{tmp_path / 'again.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, "head")
    engine = sqlalchemy.create_engine(url)
    try:
        insert_labels(engine)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("UPDATE shipments SET ever_scanned = 1 WHERE id = 3")
            )
        command.stamp(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)
        command.upgrade(config, "head")
        assert flags(engine) == {**EXPECTED, 3: True}
    finally:
        engine.dispose()


def test_upgrade_refuses_a_different_existing_column(tmp_path: Path):
    """An ever_scanned of another type fails the upgrade."""
    url = f"sqlite:///{tmp_path / 'other.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("ALTER TABLE shipments ADD COLUMN ever_scanned VARCHAR")
            )
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="shipments.ever_scanned exists"):
        command.upgrade(config, "head")


def test_downgrade_with_foreign_keys_on_keeps_events_and_links(tmp_path: Path):
    """The native drop doesn't rebuild shipments, so with foreign keys on
    its events and links survive."""
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, "head")
    engine = sqlalchemy.create_engine(url)

    def foreign_keys_on(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    sqlalchemy.event.listen(sqlalchemy.engine.Engine, "connect", foreign_keys_on)
    try:
        insert_request(engine)
        insert_labels(engine)
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO package_shipments (inmate_jurisdiction, inmate_id, "
                    "request_index, shipment_id, basis) "
                    "VALUES ('Texas', 1, 0, 1, 'unit_session')"
                )
            )
        command.downgrade(config, db_bootstrap.PRE_EVER_SCANNED_REVISION)
        assert scalar(engine, "SELECT COUNT(*) FROM tracking_events") == EVENTS
        assert scalar(engine, "SELECT COUNT(*) FROM package_shipments") == 1
    finally:
        sqlalchemy.event.remove(sqlalchemy.engine.Engine, "connect", foreign_keys_on)
        engine.dispose()


def head_without_flag(tmp_path: Path, with_labels: bool) -> str:
    """A database at head (labels stored or not) whose ever_scanned was lost."""
    url = f"sqlite:///{tmp_path / 'missing.db'}"
    command.upgrade(db_bootstrap.build_alembic_config(url), "head")
    engine = sqlalchemy.create_engine(url)
    try:
        if with_labels:
            insert_labels(engine)
        with engine.begin() as connection:
            connection.execute(sqlalchemy.text(DROP_EVER_SCANNED))
    finally:
        engine.dispose()
    return url


def test_bootstrap_refuses_a_lost_flag_with_labels_stored(tmp_path: Path, monkeypatch):
    """At head with ever_scanned gone and labels stored, startup fails: the
    flag can hold scans no stored status or event shows any more."""
    url = head_without_flag(tmp_path, with_labels=True)
    with pytest.raises(RuntimeError, match="missing at head with 6 labels stored"):
        bootstrap(monkeypatch, url)
    assert COLUMN not in columns(url)


def test_bootstrap_restores_a_lost_flag_with_no_labels(tmp_path: Path, monkeypatch):
    """With no label stored nothing is lost: the column is restored."""
    url = head_without_flag(tmp_path, with_labels=False)
    bootstrap(monkeypatch, url)
    assert COLUMN in columns(url)


def test_versioned_upgrade_still_adds_and_backfills(tmp_path: Path, monkeypatch):
    """A database at the request unit revision, labels stored, is upgraded
    by bootstrap: the column is added and backfilled, not refused."""
    url = f"sqlite:///{tmp_path / 'versioned.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url), db_bootstrap.PRE_EVER_SCANNED_REVISION
    )
    engine = sqlalchemy.create_engine(url)
    try:
        insert_labels(engine)
        bootstrap(monkeypatch, url)
        assert flags(engine) == EXPECTED
    finally:
        engine.dispose()


def test_bootstrap_backfills_a_create_all_schema_stamped_at_head(
    tmp_path: Path, monkeypatch
):
    """A create_all schema at the current models is stamped at head, which
    skips the migration's backfill, so bootstrap runs it: an older box whose
    only scan evidence is its in_transit status gets the flag."""
    url = f"sqlite:///{tmp_path / 'adopted.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        insert_labels(engine)
        assert not any(flags(engine).values())
        bootstrap(monkeypatch, url)
        assert flags(engine) == EXPECTED
    finally:
        engine.dispose()


def test_bootstrap_adopts_pre_ever_scanned_create_all_schema(
    tmp_path: Path, monkeypatch
):
    """A create_all schema with the request unit columns but no ever_scanned
    gets it, backfilled, and ends at head."""
    url = f"sqlite:///{tmp_path / 'pre.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(sqlalchemy.text(DROP_EVER_SCANNED))
        insert_labels(engine)
        bootstrap(monkeypatch, url)
        assert flags(engine) == EXPECTED
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()


def test_bootstrap_backfills_a_current_create_all_schema(tmp_path: Path, monkeypatch):
    """A create_all schema from the current models is stamped at head (its
    flags are whatever create_all's writer set)."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        assert COLUMN in columns(url)
        bootstrap(monkeypatch, url)
        assert scalar(engine, "SELECT version_num FROM alembic_version") == (
            head_revision()
        )
    finally:
        engine.dispose()
