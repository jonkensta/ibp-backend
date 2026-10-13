"""Shared pytest fixtures."""

import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

TOPLEVEL = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = TOPLEVEL / "scripts" / "migrate.py"

if str(TOPLEVEL) not in sys.path:
    sys.path.insert(0, str(TOPLEVEL))


@pytest.fixture(scope="session")
def migrate() -> ModuleType:
    """Import scripts/migrate.py (not a package) as a module."""
    spec = importlib.util.spec_from_file_location("ibp_migrate", MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEGACY_SCHEMA = """
CREATE TABLE units (
    autoid INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL,
    street1 VARCHAR NOT NULL, street2 VARCHAR, city VARCHAR NOT NULL,
    zipcode VARCHAR(12) NOT NULL, state VARCHAR(3) NOT NULL, url VARCHAR,
    jurisdiction VARCHAR(7) NOT NULL, shipping_method VARCHAR(11)
);
CREATE TABLE shipments (
    autoid INTEGER NOT NULL PRIMARY KEY, date_shipped DATE NOT NULL,
    tracking_url VARCHAR, tracking_code VARCHAR, weight INTEGER NOT NULL,
    postage INTEGER NOT NULL, unit_id INTEGER
);
CREATE TABLE inmates (
    autoid INTEGER NOT NULL PRIMARY KEY, first_name VARCHAR, last_name VARCHAR,
    jurisdiction VARCHAR(7) NOT NULL, id INTEGER NOT NULL, unit_id INTEGER,
    race VARCHAR, sex VARCHAR, release VARCHAR, url VARCHAR,
    datetime_fetched DATETIME, date_last_lookup DATE
);
CREATE TABLE lookups (
    autoid INTEGER NOT NULL PRIMARY KEY, datetime DATETIME NOT NULL,
    inmate_id INTEGER
);
CREATE TABLE comments (
    autoid INTEGER NOT NULL PRIMARY KEY, datetime DATETIME NOT NULL,
    author VARCHAR NOT NULL, body TEXT NOT NULL, inmate_id INTEGER
);
CREATE TABLE requests (
    autoid INTEGER NOT NULL PRIMARY KEY, date_processed DATE NOT NULL,
    date_postmarked DATE NOT NULL, action VARCHAR(6) NOT NULL,
    inmate_autoid INTEGER, shipment_autoid INTEGER
);
CREATE TABLE alerts (
    autoid INTEGER NOT NULL PRIMARY KEY, requester VARCHAR NOT NULL,
    email VARCHAR NOT NULL, inmate_id INTEGER
);
"""

LEGACY_ROWS: dict[str, list[tuple]] = {
    "units": [
        (1, "Alpha", "1 Main St", None, "Austin", "78701", "TX", None, "Texas", "Box"),
        (
            2,
            "Beta",
            "2 Main St",
            None,
            "Dallas",
            "75201",
            "TX",
            None,
            "Federal",
            "Federal Tub",
        ),
    ],
    "shipments": [
        (1, "2026-01-02", None, "TRACK1", 32, 450, 1),
        (2, "2026-02-03", None, "TRACK2", 16, 300, 2),
    ],
    "inmates": [
        (
            1,
            "Jane",
            "Doe",
            "Texas",
            1001,
            1,
            None,
            None,
            "2030-01-01",
            None,
            "2026-10-09 14:00:31.666803",
            "2026-10-09",
        ),
        (
            2,
            "John",
            "Roe",
            "Federal",
            2002,
            2,
            None,
            None,
            "LIFE SENTENCE",
            None,
            None,
            None,
        ),
    ],
    "lookups": [
        (1, "2026-01-15 09:30:00.000000", 1),
        (2, "2026-01-16 09:30:00.000000", None),
        (3, "2026-01-17 09:30:00.000000", 999),
    ],
    "comments": [
        (1, "2026-10-09 14:00:31.666803", "vol", 'hello, "world"\nline', 1),
        (2, "2026-03-08 02:30:00.000000", "vol", "orphan", None),
        (3, "2026-01-01 12:00:00.000000", "vol", "dangling", 42),
    ],
    "requests": [
        (10, "2026-01-05", "2025-12-30", "Filled", 1, 1),
        (11, "2026-02-05", "2026-01-30", "Tossed", 2, None),
        (12, "2026-02-06", "2026-01-31", "Filled", 2, 2),
        (13, "2026-02-07", "2026-02-01", "Filled", None, 2),
    ],
    "alerts": [(1, "someone", "someone@example.com", 1)],
}


def build_legacy_db(path: Path) -> Path:
    """Create a tiny synthetic legacy database at ``path``."""
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(LEGACY_SCHEMA)
        for table, rows in LEGACY_ROWS.items():
            placeholders = ", ".join("?" * len(rows[0]))
            connection.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})", rows  # nosec
            )
        connection.commit()
    return path


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """Path to a freshly built synthetic legacy database."""
    return build_legacy_db(tmp_path / "legacy.db")
