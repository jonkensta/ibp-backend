"""Script to import data from the legacy IBP sqlite database.

Migrates units, inmates, and per-inmate comments, requests, and lookups
from the legacy schema (integer-autoid keyed) into the new schema
(composite-keyed by (jurisdiction, id) / (inmate_jurisdiction, inmate_id, index)).

Legacy shipments, users, credentials, and alerts are intentionally dropped.
Legacy requests.shipment_autoid is dropped (no Shipment model in the new schema);
the legacy request autoid is preserved as Request.request_id.
"""

import argparse
import asyncio
import datetime
import os
import sqlite3
import sys
import typing
from contextlib import closing

import sqlalchemy
import sqlalchemy.ext.asyncio
from progressbar import ProgressBar

local_dir = os.path.dirname(os.path.realpath(__file__))  # noqa
sys.path.append(os.path.join(local_dir, os.path.pardir))  # noqa

import ibp  # pylint: disable=import-error, wrong-import-position
import ibp.db  # pylint: disable=import-error, wrong-import-position

INMATE_BATCH_SIZE = 1000


def parse_date(date: str) -> datetime.date:
    """Parse a date string."""
    return datetime.datetime.strptime(date, "%Y-%m-%d").date()


def parse_datetime(dt: str) -> datetime.datetime:
    """Parse a datetime string."""
    return datetime.datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f")


def parse_datetime_or_none(dt: str | None) -> datetime.datetime | None:
    """Parse a datetime if not none."""
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


def count_orphans(connection, table: str, inmate_column: str) -> int:
    """Count child rows whose inmate reference is NULL or dangling."""
    sql = f"""
        SELECT COUNT(*) AS count FROM {table}
        WHERE {inmate_column} IS NULL
           OR {inmate_column} NOT IN (SELECT autoid FROM inmates)
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
        # NOTE: shipping_method is passed through untouched. Legacy data may
        # contain 'Federal Tub', which requires the new model's shipping_enum
        # to include it (added on a concurrent branch).
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
                sqlalchemy.select(sqlalchemy.func.count()).select_from(model)
            )
            if count:
                raise SystemExit(
                    f"Destination table '{model.__tablename__}' is not empty "
                    f"({count} rows); refusing to migrate into a non-empty database."
                )


async def migrate_units(connection, session_factory) -> None:
    """Migrate the units table."""
    print("Adding units")
    units = generate_units(connection)
    progress = ProgressBar(max_value=table_length(connection, "units"))
    units = progress(units)

    async with session_factory() as session:
        session.add_all(ibp.models.Unit(**unit) for unit in units)
        await session.commit()


async def migrate_inmates(connection, session_factory) -> None:
    """Migrate inmates along with their comments, requests, and lookups."""
    print("Adding inmates (with comments, requests, and lookups)")
    unit_names = build_unit_names(connection)
    inmates = generate_inmates(connection, unit_names)
    progress = ProgressBar(max_value=table_length(connection, "inmates"))
    inmates = progress(inmates)

    async with session_factory() as session:
        batch = 0
        for inmate in inmates:
            session.add(ibp.models.Inmate(**inmate))
            batch += 1
            if batch >= INMATE_BATCH_SIZE:
                await session.commit()
                batch = 0
        await session.commit()


async def verify_counts(connection, session_factory) -> None:
    """Compare source and destination row counts and fail loudly on mismatch.

    Child rows with a NULL or dangling inmate reference cannot be represented
    in the new schema (children are keyed by their inmate); they are reported
    explicitly and excluded from the expected counts.
    """
    orphan_columns = {
        "comments": "inmate_id",
        "lookups": "inmate_id",
        "requests": "inmate_autoid",
    }
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
                sqlalchemy.select(sqlalchemy.func.count()).select_from(model)
            )
            print(f"{table:<10} {source:>8} {orphaned:>9} {expected:>9} {migrated:>9}")
            if migrated != expected:
                failures.append(f"{table}: expected {expected}, migrated {migrated}")

    total_orphans = sum(
        count_orphans(connection, table, column)
        for table, column in orphan_columns.items()
    )
    if total_orphans:
        print(
            f"\nWARNING: {total_orphans} legacy child rows have a NULL or dangling "
            "inmate reference and were NOT migrated (see 'orphaned' column above)."
        )

    if failures:
        raise SystemExit("Row count verification FAILED:\n  " + "\n  ".join(failures))

    print("\nRow count verification passed.")


async def main():
    """Import data from the legacy IBP database."""

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("filepath", help="legacy sqlite database filepath")
    args = parser.parse_args()

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
            sqlite3.connect(f"file:{args.filepath}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = dict_factory

            await migrate_units(connection, session_factory)
            await migrate_inmates(connection, session_factory)
            await verify_counts(connection, session_factory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
