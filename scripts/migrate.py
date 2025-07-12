"""Script to import data from the IBP database."""

import argparse
import asyncio
import datetime
import os
import sqlite3
import sys
import typing
from contextlib import asynccontextmanager, closing

from progressbar import ProgressBar

local_dir = os.path.dirname(os.path.realpath(__file__))  # noqa
sys.path.append(os.path.join(local_dir, os.path.pardir))  # noqa

import ibp  # pylint: disable=import-error, wrong-import-position
import ibp.db


def parse_date(date: str) -> datetime.date:
    """Parse a date string."""
    return datetime.datetime.strptime(date, "%Y-%m-%d").date()


def parse_date_or_none(date: str | None) -> datetime.date | None:
    """Parse a date string if not none."""
    return parse_date(date) if date is not None else None


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


def units_length(connection) -> int:
    """Return length of units table."""
    sql = "SELECT COUNT(*) FROM units"
    return connection.execute(sql).fetchone()["COUNT(*)"]


def generate_units(connection) -> typing.Iterable[dict]:
    """Generate all units from units table."""
    sql = "SELECT * FROM units"
    for unit in connection.execute(sql):
        unit.pop("autoid")
        yield unit


def generate_comments(connection, inmate_autoid) -> typing.Iterable[dict]:
    """Generate comments from the comments table."""
    sql = f"""
        SELECT * FROM comments
        WHERE inmate_id = {inmate_autoid}
        ORDER BY datetime ASC
    """
    comments = connection.execute(sql)
    for index, comment in enumerate(comments):
        comment.pop("inmate_id")
        comment.pop("autoid")

        comment["index"] = index
        comment["datetime"] = parse_datetime(comment["datetime"])

        yield comment


def shipments_length(connection) -> int:
    """Get the length of the shipments table."""
    sql = "SELECT COUNT(*) FROM shipments"
    return connection.execute(sql).fetchone()["COUNT(*)"]


def generate_shipments(connection) -> typing.Iterable[dict]:
    """Generate shipments from the shipments table."""
    shipments = connection.execute("SELECT * FROM shipments")
    for shipment in shipments:
        shipment["id"] = shipment.pop("autoid")
        shipment["date_shipped"] = parse_date(shipment["date_shipped"])
        yield shipment


def generate_requests(connection, inmate_autoid) -> typing.Iterable[dict]:
    """Generate requests from the requests table."""
    sql = f"""
        SELECT * FROM requests
        WHERE inmate_autoid = {inmate_autoid}
        ORDER BY date_postmarked ASC
    """
    requests = connection.execute(sql)

    for index, request in enumerate(requests):
        request.pop("inmate_autoid")
        request["id"] = request.pop("autoid")
        request["index"] = index
        request["date_processed"] = parse_date(request["date_processed"])
        request["date_postmarked"] = parse_date(request["date_postmarked"])
        request["shipment_id"] = request.pop("shipment_autoid")
        yield request


def inmates_length(connection) -> int:
    """Get the length of the inmates table."""
    sql = "SELECT COUNT(*) FROM inmates"
    return connection.execute(sql).fetchone()["COUNT(*)"]


def generate_inmates(connection) -> typing.Iterable[dict]:
    """Generate inmates from the inmates table."""
    inmates_sql = "SELECT * FROM inmates"
    for inmate in connection.execute(inmates_sql):
        autoid = inmate.pop("autoid")
        inmate.pop("date_last_lookup")
        inmate["datetime_fetched"] = parse_datetime_or_none(inmate["datetime_fetched"])
        inmate["comments"] = list(generate_comments(connection, autoid))
        inmate["requests"] = list(generate_requests(connection, autoid))
        yield inmate


async def create_db():
    """Create the sqlalchemy database."""
    async with ibp.db.build_engine().begin() as conn:
        await conn.run_sync(ibp.db.Base.metadata.create_all)


async def main():
    """Import data from IBP database"""

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("filepath", help="database filepath")
    args = parser.parse_args()

    await create_db()

    with closing(sqlite3.connect(args.filepath)) as connection:
        connection.row_factory = dict_factory

        print("Adding units")
        units = generate_units(connection)
        progress = ProgressBar(max_value=units_length(connection))
        units = progress(units)

        async with ibp.db.async_session() as session:
            units = (ibp.models.Unit(**unit) for unit in units)
            session.add_all(units)
            await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
