"""End-to-end test of scripts/migrate.py against a synthetic legacy database."""

import csv
import datetime
import os
import subprocess  # nosec: runs the repo's own script
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from alembic.config import Config
from alembic.script import ScriptDirectory

import ibp.models

from .conftest import MIGRATE_SCRIPT, TOPLEVEL

UTC = datetime.timezone.utc


def run_migrate(legacy: Path, destination: Path, *extra: str):
    """Run the migration script in a subprocess against ``destination``."""
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{destination}")
    env.pop("CONF", None)
    return subprocess.run(  # nosec: fixed argv
        [sys.executable, str(MIGRATE_SCRIPT), str(legacy), *extra],
        cwd=TOPLEVEL,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(name="migrated")
def fixture_migrated(legacy_db: Path, tmp_path: Path):
    """Run the migration once and return (engine, archive dir, stdout)."""
    destination = tmp_path / "new.db"
    archive = tmp_path / "archive"
    result = run_migrate(legacy_db, destination, "--archive-dir", str(archive))
    assert result.returncode == 0, result.stdout + result.stderr
    engine = sqlalchemy.create_engine(f"sqlite:///{destination}")
    yield engine, archive, result.stdout
    engine.dispose()


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV file into a list of dicts."""
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_schema_is_stamped_at_alembic_head(migrated):
    """The new database is ready without a separate bootstrap step."""
    engine, _, _ = migrated
    config = Config(str(TOPLEVEL / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        versions = connection.execute(
            sqlalchemy.text("SELECT version_num FROM alembic_version")
        ).scalars()
        assert list(versions) == [head]


def test_datetimes_are_converted_and_dates_are_not(migrated):
    """DATETIME columns shift from US Central to UTC; DATE columns do not."""
    engine, _, _ = migrated
    expected = datetime.datetime(2026, 10, 9, 19, 0, 31, 666803, tzinfo=UTC)
    with Session(engine) as session:
        inmate = session.get(ibp.models.Inmate, ("Texas", 1001))
        assert inmate is not None
        assert inmate.datetime_fetched == expected
        assert [c.datetime_created for c in inmate.comments] == [expected]
        assert [lk.datetime_created for lk in inmate.lookups] == [
            datetime.datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
        ]
        (request,) = inmate.requests
        assert request.request_id == 10
        assert request.date_processed == datetime.date(2026, 1, 5)
        assert request.date_postmarked == datetime.date(2025, 12, 30)


def test_units_including_federal_tub_are_migrated(migrated):
    """Legacy 'Federal Tub' shipping methods survive the migration."""
    engine, _, _ = migrated
    with Session(engine) as session:
        unit = session.get(ibp.models.Unit, ("Federal", "Beta"))
        assert unit is not None
        assert unit.shipping_method == "Federal Tub"


def test_archive_exports_non_migrated_data(migrated):
    """Shipments, request links and orphans are archived as CSV."""
    _, archive, stdout = migrated
    shipments = read_csv(archive / "shipments.csv")
    assert [row["tracking_code"] for row in shipments] == ["TRACK1", "TRACK2"]
    assert read_csv(archive / "request_shipments.csv") == [
        {"request_autoid": "10", "shipment_autoid": "1"},
        {"request_autoid": "12", "shipment_autoid": "2"},
        {"request_autoid": "13", "shipment_autoid": "2"},
    ]
    assert [r["autoid"] for r in read_csv(archive / "orphan_comments.csv")] == [
        "2",
        "3",
    ]
    assert [r["autoid"] for r in read_csv(archive / "orphan_lookups.csv")] == [
        "2",
        "3",
    ]
    assert [r["autoid"] for r in read_csv(archive / "orphan_requests.csv")] == ["13"]
    assert "shipments.csv (2 rows)" in stdout
    assert "Migration summary" in stdout


def test_refuses_non_empty_destination(legacy_db: Path, tmp_path: Path):
    """A second run into the same database fails without touching it."""
    destination = tmp_path / "new.db"
    assert run_migrate(legacy_db, destination).returncode == 0
    second = run_migrate(legacy_db, destination)
    assert second.returncode != 0
    assert "refusing to migrate" in second.stderr
