"""The migration rewriting Texas race and sex codes as words, and bootstrap."""

import sqlite3
from contextlib import closing
from pathlib import Path

import sqlalchemy
from alembic import command

import ibp.models
from ibp import db_bootstrap

from .conftest import TOPLEVEL
from .test_migrate_end_to_end import run_migrate
from .test_package_items_migration import head_revision, scalar

MIGRATION = (
    TOPLEVEL / "alembic" / "versions" / "d2a6f8c3e1b7_texas_race_sex_codes_to_words.py"
)

# (jurisdiction, id, race, sex) before the migration, and race and sex after.
ROWS = [
    ("Texas", 1, "W", "M", "White", "Male"),
    ("Texas", 2, "B", "F", "Black", "Female"),
    ("Texas", 3, "H", "U", "Hispanic", "Unknown"),
    ("Texas", 4, "A", "M", "Asian", "Male"),
    ("Texas", 5, "I", "F", "American Indian", "Female"),
    ("Texas", 6, "O", "M", "Other", "Male"),
    ("Texas", 7, "U", "F", "Unknown", "Female"),
    # Unseen values and no data are left alone.
    ("Texas", 8, "X", "Q", "X", "Q"),
    ("Texas", 9, None, None, None, None),
    # Federal rows hold words already, and even a code there is not touched.
    ("Federal", 10, "White", "Male", "White", "Male"),
    ("Federal", 11, "Black", "Female", "Black", "Female"),
    ("Federal", 12, "American Indian", None, "American Indian", None),
    ("Federal", 13, "W", "M", "W", "M"),
]


def insert_rows(engine: sqlalchemy.Engine, rows) -> None:
    """Insert inmates as (jurisdiction, id, race, sex, ...) using plain SQL."""
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO inmates (jurisdiction, id, first_name, race, sex) "
                "VALUES (:jurisdiction, :id, 'JANE', :race, :sex)"
            ),
            [
                {"jurisdiction": row[0], "id": row[1], "race": row[2], "sex": row[3]}
                for row in rows
            ],
        )


def stored(engine: sqlalchemy.Engine) -> list[tuple]:
    """Every inmate's (jurisdiction, id, race, sex), in id order."""
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                sqlalchemy.text(
                    "SELECT jurisdiction, id, race, sex FROM inmates ORDER BY id"
                )
            )
        ]


def before(rows) -> list[tuple]:
    """The rows as inserted."""
    return [row[:4] for row in rows]


def after(rows) -> list[tuple]:
    """The rows as the migration should leave them."""
    return [row[:2] + row[4:] for row in rows]


def assert_at_head(engine: sqlalchemy.Engine) -> None:
    """Assert the database is stamped at the alembic head."""
    version = scalar(engine, "SELECT version_num FROM alembic_version")
    assert version == head_revision()


def bootstrap(monkeypatch, url: str) -> None:
    """Run db_bootstrap against ``url``."""
    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()


def test_migration_follows_pronouns_as_the_only_head():
    """The revision is the single head, right after the pronouns one."""
    assert head_revision() == "d2a6f8c3e1b7"
    source = MIGRATION.read_text(encoding="utf-8")
    assert "down_revision: Union[str, None] = 'c4e7a2f9d1b3'" in source
    assert db_bootstrap.PRE_RACE_SEX_WORDS_REVISION == "c4e7a2f9d1b3"
    # A migration keeps its own copy of the mapping.
    assert "import ibp" not in source and "from ibp" not in source


def test_migration_up_and_down(tmp_path: Path):
    """Upgrading rewrites Texas codes; downgrading puts exactly them back."""
    url = f"sqlite:///{tmp_path / 'racesex.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_RACE_SEX_WORDS_REVISION)
    engine = sqlalchemy.create_engine(url)
    try:
        insert_rows(engine, ROWS)
        command.upgrade(config, "head")
        assert stored(engine) == after(ROWS)

        command.downgrade(config, db_bootstrap.PRE_RACE_SEX_WORDS_REVISION)
        assert stored(engine) == before(ROWS)
    finally:
        engine.dispose()


def test_bootstrap_upgrades_database_at_previous_head(tmp_path: Path, monkeypatch):
    """A database at the pronouns revision gets the rewrite on startup."""
    url = f"sqlite:///{tmp_path / 'previous.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url),
        db_bootstrap.PRE_RACE_SEX_WORDS_REVISION,
    )
    engine = sqlalchemy.create_engine(url)
    try:
        insert_rows(engine, ROWS)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(ROWS)
        assert_at_head(engine)
    finally:
        engine.dispose()


def test_bootstrap_leaves_canonical_words(tmp_path: Path, monkeypatch):
    """A database that already holds words keeps them, and running again is a
    no-op."""
    url = f"sqlite:///{tmp_path / 'words.db'}"
    command.upgrade(
        db_bootstrap.build_alembic_config(url),
        db_bootstrap.PRE_RACE_SEX_WORDS_REVISION,
    )
    rows = [row[:2] + row[4:] * 2 for row in ROWS]
    engine = sqlalchemy.create_engine(url)
    try:
        insert_rows(engine, rows)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(rows)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(rows)
        assert_at_head(engine)
    finally:
        engine.dispose()


def test_bootstrap_rewrites_codes_in_current_create_all_schema(
    tmp_path: Path, monkeypatch
):
    """A create_all schema from the current models is not stamped past the
    rewrite: codes it holds become words, and it ends at head."""
    url = f"sqlite:///{tmp_path / 'current.db'}"
    engine = sqlalchemy.create_engine(url)
    try:
        ibp.models.Base.metadata.create_all(engine)
        insert_rows(engine, ROWS)
        bootstrap(monkeypatch, url)
        assert stored(engine) == after(ROWS)
        assert_at_head(engine)
    finally:
        engine.dispose()


def test_bootstrap_builds_new_database_at_head(tmp_path: Path, monkeypatch):
    """A brand-new database is built to head."""
    url = f"sqlite:///{tmp_path / 'new.db'}"
    bootstrap(monkeypatch, url)
    engine = sqlalchemy.create_engine(url)
    try:
        assert_at_head(engine)
        assert scalar(engine, "SELECT COUNT(*) FROM inmates") == 0
    finally:
        engine.dispose()


def test_legacy_migration_rewrites_texas_codes(legacy_db: Path, tmp_path: Path):
    """scripts/migrate.py copies legacy codes, which end up as words."""
    with closing(sqlite3.connect(legacy_db)) as connection:
        connection.execute(
            "UPDATE inmates SET race = 'H', sex = 'F' WHERE jurisdiction = 'Texas'"
        )
        connection.execute(
            "UPDATE inmates SET race = 'Black', sex = 'Male' "
            "WHERE jurisdiction = 'Federal'"
        )
        connection.commit()

    destination = tmp_path / "new.db"
    result = run_migrate(legacy_db, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    engine = sqlalchemy.create_engine(f"sqlite:///{destination}")
    try:
        assert stored(engine) == [
            ("Texas", 1001, "Hispanic", "Female"),
            ("Federal", 2002, "Black", "Male"),
        ]
        assert_at_head(engine)
    finally:
        engine.dispose()
