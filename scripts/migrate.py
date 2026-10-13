"""Script to import data from the legacy IBP sqlite database.

Usage::

    DATABASE_URL=sqlite+aiosqlite:///path/new.db \\
        uv run --group dev scripts/migrate.py legacy.db [--archive-dir DIR]

With ``--archive-dir``, legacy data that is not migrated (the shipments table,
the request -> shipment linkage, and orphaned child rows) is exported to CSV
files in DIR for safekeeping; this does not change what gets migrated.

The destination is the database configured for the app (``DATABASE_URL`` via
the server config).  It must be empty or not exist yet.  On success the
schema is stamped with the current alembic revision (via
``ibp.db_bootstrap``), so the resulting database is ready for the app as-is;
no separate ``python -m ibp.db_bootstrap`` step is needed.  Running it
anyway (e.g. from the container entrypoint) is harmless: it sees the
alembic_version table and performs a no-op ``alembic upgrade head``.

Migrates units, inmates, and per-inmate comments, requests, and lookups
from the legacy schema (integer-autoid keyed) into the new schema
(composite-keyed by (jurisdiction, id) / (inmate_jurisdiction, inmate_id, index)).

Legacy shipments, users, credentials, and alerts are intentionally dropped.
Legacy requests.shipment_autoid is dropped (no Shipment model in the new schema);
the legacy request autoid is preserved as Request.request_id.

Timezones: the legacy app stored NAIVE datetimes in US Central local time
(``datetime.now()`` on a server running in America/Chicago), whereas the new
schema's TZDateTime treats naive values as UTC.  Every legacy DATETIME column
that is migrated (comments.datetime, lookups.datetime, inmates.datetime_fetched)
is therefore interpreted as America/Chicago wall-clock time and converted to a
UTC-aware datetime before insertion.  DATE-only columns (requests.date_processed,
requests.date_postmarked) and the free-form inmates.release string are NOT shifted.

Around DST transitions the wall-clock time is resolved with ``fold=0``:

- ambiguous times (the repeated hour when clocks fall back, e.g. 01:30 on the
  first Sunday of November) resolve to the FIRST occurrence, i.e. CDT (UTC-5);
- nonexistent times (the skipped hour when clocks spring forward, e.g. 02:30
  on the second Sunday of March) are interpreted with the pre-transition
  offset, CST (UTC-6), which is the same instant as 03:30 CDT.
"""

import argparse
import asyncio
import csv
import datetime
import os
import sqlite3
import sys
import typing
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy
import sqlalchemy.ext.asyncio
from progressbar import ProgressBar

local_dir = os.path.dirname(os.path.realpath(__file__))  # noqa
sys.path.append(os.path.join(local_dir, os.path.pardir))  # noqa

import ibp  # pylint: disable=import-error, wrong-import-position
import ibp.db  # pylint: disable=import-error, wrong-import-position
import ibp.db_bootstrap  # pylint: disable=import-error, wrong-import-position

INMATE_BATCH_SIZE = 1000

LEGACY_TIMEZONE = ZoneInfo("America/Chicago")

# Legacy tables that are intentionally not migrated (reported in the summary).
UNMIGRATED_TABLES = ("shipments", "users", "credentials", "alerts")

# Legacy child tables and the column referencing their inmate's autoid.
ORPHAN_COLUMNS = {
    "comments": "inmate_id",
    "lookups": "inmate_id",
    "requests": "inmate_autoid",
}


def parse_date(date: str) -> datetime.date:
    """Parse a date string."""
    return datetime.datetime.strptime(date, "%Y-%m-%d").date()


def legacy_local_to_utc(naive: datetime.datetime) -> datetime.datetime:
    """Convert a naive legacy US Central wall-clock datetime to aware UTC.

    DST edge cases are resolved with ``fold=0`` (see the module docstring).
    """
    if naive.tzinfo is not None:
        raise ValueError(f"expected a naive legacy datetime, got {naive!r}")
    local = naive.replace(tzinfo=LEGACY_TIMEZONE, fold=0)
    return local.astimezone(datetime.timezone.utc)


def parse_datetime(dt: str) -> datetime.datetime:
    """Parse a legacy (naive, US Central) datetime string into aware UTC."""
    naive = datetime.datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f")
    return legacy_local_to_utc(naive)


def parse_datetime_or_none(dt: str | None) -> datetime.datetime | None:
    """Parse a legacy datetime string into aware UTC if not none."""
    return parse_datetime(dt) if dt is not None else None


def dict_factory(cursor, row) -> dict:
    """Row factory for returning a dictionary."""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def table_length(connection, table: str) -> int:
    """Return the number of rows in a legacy table."""
    sql = f"SELECT COUNT(*) AS count FROM {table}"  # nosec: fixed table names
    return connection.execute(sql).fetchone()["count"]


def orphan_condition(inmate_column: str) -> str:
    """SQL condition matching child rows with a NULL or dangling inmate."""
    return (
        f"{inmate_column} IS NULL "
        f"OR {inmate_column} NOT IN (SELECT autoid FROM inmates)"
    )


def count_orphans(connection, table: str, inmate_column: str) -> int:
    """Count child rows whose inmate reference is NULL or dangling."""
    sql = f"""
        SELECT COUNT(*) AS count FROM {table}
        WHERE {orphan_condition(inmate_column)}
    """  # nosec: fixed table/column names
    return connection.execute(sql).fetchone()["count"]


def build_unit_names(connection) -> dict[int, str]:
    """Map legacy units.autoid to unit name (the new schema keys units by name)."""
    sql = "SELECT autoid, name FROM units"
    return {row["autoid"]: row["name"] for row in connection.execute(sql)}


def generate_units(connection) -> typing.Iterable[dict]:
    """Generate all units from the legacy units table."""
    sql = "SELECT * FROM units"
    for unit in connection.execute(sql):
        unit.pop("autoid")
        yield unit


def fetch_child_rows(
    connection, table: str, inmate_column: str, order_column: str
) -> dict[int, list[dict]]:
    """Fetch a child table once, grouped by legacy inmate autoid.

    The legacy database has no indexes on the child tables, so per-inmate
    queries would require a full table scan per inmate; a single grouped
    pass keeps the migration linear.
    """
    sql = f"""
        SELECT * FROM {table}
        WHERE {inmate_column} IS NOT NULL
        ORDER BY {order_column} ASC, autoid ASC
    """  # nosec: fixed table/column names
    grouped: dict[int, list[dict]] = {}
    for row in connection.execute(sql):
        grouped.setdefault(row.pop(inmate_column), []).append(row)
    return grouped


def convert_comments(rows: list[dict]) -> typing.Iterable[dict]:
    """Convert one inmate's legacy comment rows to new-schema kwargs."""
    for index, comment in enumerate(rows):
        comment.pop("autoid")
        comment["index"] = index
        comment["datetime_created"] = parse_datetime(comment.pop("datetime"))
        yield comment


def convert_lookups(rows: list[dict]) -> typing.Iterable[dict]:
    """Convert one inmate's legacy lookup rows to new-schema kwargs."""
    for index, lookup in enumerate(rows):
        lookup.pop("autoid")
        lookup["index"] = index
        lookup["datetime_created"] = parse_datetime(lookup.pop("datetime"))
        yield lookup


def convert_requests(rows: list[dict]) -> typing.Iterable[dict]:
    """Convert one inmate's legacy request rows to new-schema kwargs."""
    for index, request in enumerate(rows):
        # Shipments are dropped in the new schema; discard the linkage.
        request.pop("shipment_autoid", None)

        # Preserve the legacy autoid as request_id (legacy-id bridge).
        request["request_id"] = request.pop("autoid")
        request["index"] = index
        request["date_processed"] = parse_date(request["date_processed"])
        request["date_postmarked"] = parse_date(request["date_postmarked"])

        yield request


def generate_inmates(connection, unit_names: dict[int, str]) -> typing.Iterable[dict]:
    """Generate inmates (with nested children) from the legacy inmates table."""
    comments = fetch_child_rows(connection, "comments", "inmate_id", "datetime")
    lookups = fetch_child_rows(connection, "lookups", "inmate_id", "datetime")
    requests = fetch_child_rows(
        connection, "requests", "inmate_autoid", "date_postmarked"
    )

    inmates_sql = "SELECT * FROM inmates"
    for inmate in connection.execute(inmates_sql):
        autoid = inmate.pop("autoid")

        # Dropped in the new schema (lookups relationship supersedes it).
        inmate.pop("date_last_lookup")

        # The new schema references units by (jurisdiction, name).
        unit_id = inmate.pop("unit_id")
        inmate["unit_name"] = unit_names[unit_id] if unit_id is not None else None

        inmate["datetime_fetched"] = parse_datetime_or_none(inmate["datetime_fetched"])
        # 'release' passes through as-is: it is a free-form string in both
        # schemas (isoformat date or e.g. 'LIFE SENTENCE', 'DEATH ROW').

        inmate["comments"] = [
            ibp.models.Comment(**comment)
            for comment in convert_comments(comments.pop(autoid, []))
        ]
        inmate["requests"] = [
            ibp.models.Request(**request)
            for request in convert_requests(requests.pop(autoid, []))
        ]
        inmate["lookups"] = [
            ibp.models.Lookup(**lookup)
            for lookup in convert_lookups(lookups.pop(autoid, []))
        ]

        yield inmate


async def create_db(engine) -> None:
    """Create the sqlalchemy database."""
    async with engine.begin() as conn:
        await conn.run_sync(ibp.db.Base.metadata.create_all)


async def assert_destination_empty(session_factory) -> None:
    """Fail loudly if the destination database already contains data."""
    async with session_factory() as session:
        for model in (ibp.models.Unit, ibp.models.Inmate):
            count = await session.scalar(
                sqlalchemy.select(
                    sqlalchemy.func.count()  # pylint: disable=not-callable
                ).select_from(model)
            )
            if count:
                raise SystemExit(
                    f"Destination table '{model.__tablename__}' is not empty "
                    f"({count} rows); refusing to migrate into a non-empty database."
                )


async def migrate_units(connection, session_factory) -> None:
    """Migrate the units table."""
    print("Adding units")
    progress = ProgressBar(max_value=table_length(connection, "units"))
    units = progress(generate_units(connection))

    async with session_factory() as session:
        session.add_all(ibp.models.Unit(**unit) for unit in units)
        await session.commit()


async def migrate_inmates(connection, session_factory) -> None:
    """Migrate inmates along with their comments, requests, and lookups."""
    print("Adding inmates (with comments, requests, and lookups)")
    unit_names = build_unit_names(connection)
    progress = ProgressBar(max_value=table_length(connection, "inmates"))
    inmates = progress(generate_inmates(connection, unit_names))

    async with session_factory() as session:
        batch = 0
        for inmate in inmates:
            session.add(ibp.models.Inmate(**inmate))
            batch += 1
            if batch >= INMATE_BATCH_SIZE:
                await session.commit()
                batch = 0
        await session.commit()


class MigrationReport(typing.NamedTuple):
    """Row counts gathered while migrating, for the final summary."""

    # table -> (source rows, orphaned rows dropped, rows migrated)
    counts: dict[str, tuple[int, int, int]]
    # legacy table -> rows (tables that are not migrated at all)
    unmigrated: dict[str, int]
    # archive CSV path -> data rows written
    archived: list[tuple[Path, int]]


def legacy_table_exists(connection, table: str) -> bool:
    """Return whether the legacy database has the given table."""
    sql = "SELECT COUNT(*) AS count FROM sqlite_master WHERE type='table' AND name=?"
    return bool(connection.execute(sql, (table,)).fetchone()["count"])


def count_unmigrated(connection) -> dict[str, int]:
    """Count rows in legacy tables that are not migrated."""
    return {
        table: table_length(connection, table)
        for table in UNMIGRATED_TABLES
        if legacy_table_exists(connection, table)
    }


async def verify_counts(connection, session_factory) -> dict[str, tuple[int, int, int]]:
    """Compare source and destination row counts and fail loudly on mismatch.

    Child rows with a NULL or dangling inmate reference cannot be represented
    in the new schema (children are keyed by their inmate); they are reported
    explicitly and excluded from the expected counts.
    """
    orphan_columns = ORPHAN_COLUMNS
    models = {
        "units": ibp.models.Unit,
        "inmates": ibp.models.Inmate,
        "comments": ibp.models.Comment,
        "requests": ibp.models.Request,
        "lookups": ibp.models.Lookup,
    }

    print("\nVerifying row counts (source vs destination):")
    header = (
        f"{'table':<10} {'source':>8} {'orphaned':>9} {'expected':>9} {'migrated':>9}"
    )
    print(header)
    print("-" * len(header))

    failures = []
    counts = {}
    async with session_factory() as session:
        for table, model in models.items():
            source = table_length(connection, table)
            orphaned = (
                count_orphans(connection, table, orphan_columns[table])
                if table in orphan_columns
                else 0
            )
            expected = source - orphaned
            migrated = await session.scalar(
                sqlalchemy.select(
                    sqlalchemy.func.count()  # pylint: disable=not-callable
                ).select_from(model)
            )
            print(f"{table:<10} {source:>8} {orphaned:>9} {expected:>9} {migrated:>9}")
            counts[table] = (source, orphaned, migrated)
            if migrated != expected:
                failures.append(f"{table}: expected {expected}, migrated {migrated}")

    total_orphans = sum(orphaned for _, orphaned, _ in counts.values())
    if total_orphans:
        print(
            f"\nWARNING: {total_orphans} legacy child rows have a NULL or dangling "
            "inmate reference and were NOT migrated (see 'orphaned' column above)."
        )

    if failures:
        raise SystemExit("Row count verification FAILED:\n  " + "\n  ".join(failures))

    print("\nRow count verification passed.")
    return counts


def export_query_csv(connection, sql: str, path: Path) -> int:
    """Write the result of a legacy query to a UTF-8 CSV with a header row."""
    cursor = connection.execute(sql)
    header = [column[0] for column in cursor.description]
    count = 0
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for row in cursor:
            writer.writerow(row[column] for column in header)
            count += 1
    return count


def archive_queries() -> dict[str, str]:
    """Map archive CSV filenames to the legacy queries that produce them."""
    queries = {
        "shipments.csv": "SELECT * FROM shipments ORDER BY autoid",
        "request_shipments.csv": """
            SELECT autoid AS request_autoid, shipment_autoid FROM requests
            WHERE shipment_autoid IS NOT NULL ORDER BY autoid
        """,
    }
    for table, column in ORPHAN_COLUMNS.items():
        queries[f"orphan_{table}.csv"] = f"""
            SELECT * FROM {table} WHERE {orphan_condition(column)}
            ORDER BY autoid
        """  # nosec: fixed table/column names
    return queries


def export_archive(connection, archive_dir: Path) -> list[tuple[Path, int]]:
    """Export legacy data that is not migrated to CSV files in ``archive_dir``.

    Writes the full shipments table, the request -> shipment linkage
    (request_autoid equals the migrated Request.request_id), and the orphaned
    comments/lookups/requests rows (NULL or dangling inmate reference) that
    the migration drops.  Values are written exactly as stored in the legacy
    database, i.e. datetimes are naive US Central local time.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for name, sql in archive_queries().items():
        path = archive_dir / name
        exported.append((path, export_query_csv(connection, sql, path)))
    return exported


def print_summary(report: MigrationReport) -> None:
    """Print the final migration summary."""
    print("\nMigration summary")
    print("=================")
    print("Rows migrated (orphans dropped):")
    for table, (_, orphaned, migrated) in report.counts.items():
        print(f"  {table:<12} {migrated:>8} migrated  {orphaned:>6} orphans dropped")
    total_orphans = sum(orphaned for _, orphaned, _ in report.counts.values())
    print(f"  {'total':<12} {'':>8}           {total_orphans:>6} orphans dropped")

    if report.unmigrated:
        print("Legacy tables not migrated:")
        for table, count in report.unmigrated.items():
            print(f"  {table:<12} {count:>8} rows")

    if report.archived:
        print("Archived legacy data that is not migrated (CSV, UTF-8):")
        for path, count in report.archived:
            print(f"  {path} ({count} rows)")
    else:
        print("No archive written (pass --archive-dir DIR to export one).")


async def run_migration(
    filepath: str, archive_dir: Path | None = None
) -> MigrationReport:
    """Create the schema and migrate the legacy data into it.

    If ``archive_dir`` is given, also export non-migrated legacy data to CSV.
    """
    engine = ibp.db.build_engine()
    session_factory = sqlalchemy.ext.asyncio.async_sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        class_=sqlalchemy.ext.asyncio.AsyncSession,
    )

    try:
        await create_db(engine)
        await assert_destination_empty(session_factory)

        with closing(
            sqlite3.connect(f"file:{filepath}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = dict_factory

            await migrate_units(connection, session_factory)
            await migrate_inmates(connection, session_factory)
            counts = await verify_counts(connection, session_factory)
            unmigrated = count_unmigrated(connection)
            archived = (
                export_archive(connection, archive_dir)
                if archive_dir is not None
                else []
            )
    finally:
        await engine.dispose()

    return MigrationReport(counts, unmigrated, archived)


def stamp_schema() -> None:
    """Record the schema version so the database is ready for the app.

    The schema was just created by ``Base.metadata.create_all`` from the
    current models, so ``ibp.db_bootstrap`` stamps it as alembic ``head``
    (or, if alembic_version already exists, runs a no-op upgrade).
    """
    print("\nStamping alembic schema version (ibp.db_bootstrap)")
    ibp.db_bootstrap.bootstrap()


def main() -> None:
    """Import data from the legacy IBP database."""

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("filepath", help="legacy sqlite database filepath")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        help=(
            "write CSV exports of legacy data that is not migrated (shipments, "
            "request->shipment links, orphaned child rows) to this directory"
        ),
    )
    args = parser.parse_args()

    report = asyncio.run(run_migration(args.filepath, args.archive_dir))
    stamp_schema()
    print_summary(report)


if __name__ == "__main__":
    main()
