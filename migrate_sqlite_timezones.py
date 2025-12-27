#!/usr/bin/env python3
"""
Migrate SQLite datetime values to be timezone-aware (UTC).

This script updates all existing datetime columns in data.db to include
timezone information (UTC), making them compatible with PostgreSQL TIMESTAMPTZ.
"""

import asyncio
import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from ibp.models import Inmate, Lookup, Comment
from ibp.db import Base


async def migrate_timezones():
    """Migrate all datetime columns to be timezone-aware."""
    # Connect to SQLite
    uri = "sqlite+aiosqlite:///data.db"
    engine = create_async_engine(uri, connect_args={"check_same_thread": False})
    SessionMaker = async_sessionmaker(engine, class_=AsyncSession)

    print("=" * 60)
    print("SQLite Timezone Migration")
    print("=" * 60)
    print(f"\nDatabase: {uri}")
    print("\nMigrating datetime columns to UTC timezone-aware...\n")

    async with SessionMaker() as session:
        # Migrate Inmate.datetime_fetched
        print("Migrating Inmate.datetime_fetched...")
        result = await session.execute(select(Inmate))
        inmates = result.scalars().all()

        updated_inmates = 0
        for inmate in inmates:
            if inmate.datetime_fetched is not None and inmate.datetime_fetched.tzinfo is None:
                inmate.datetime_fetched = inmate.datetime_fetched.replace(
                    tzinfo=datetime.timezone.utc
                )
                updated_inmates += 1

        print(f"  Updated {updated_inmates}/{len(inmates)} Inmate records")

        # Migrate Lookup.datetime_created
        print("\nMigrating Lookup.datetime_created...")
        result = await session.execute(select(Lookup))
        lookups = result.scalars().all()

        updated_lookups = 0
        for lookup in lookups:
            if lookup.datetime_created.tzinfo is None:
                lookup.datetime_created = lookup.datetime_created.replace(
                    tzinfo=datetime.timezone.utc
                )
                updated_lookups += 1

        print(f"  Updated {updated_lookups}/{len(lookups)} Lookup records")

        # Migrate Comment.datetime_created
        print("\nMigrating Comment.datetime_created...")
        result = await session.execute(select(Comment))
        comments = result.scalars().all()

        updated_comments = 0
        for comment in comments:
            if comment.datetime_created.tzinfo is None:
                comment.datetime_created = comment.datetime_created.replace(
                    tzinfo=datetime.timezone.utc
                )
                updated_comments += 1

        print(f"  Updated {updated_comments}/{len(comments)} Comment records")

        # Commit changes
        print("\nCommitting changes...")
        await session.commit()
        print("✓ All changes committed")

    await engine.dispose()

    print("\n" + "=" * 60)
    print("✓ Migration complete!")
    print("=" * 60)
    print(f"\nTotal records updated:")
    print(f"  Inmates: {updated_inmates}")
    print(f"  Lookups: {updated_lookups}")
    print(f"  Comments: {updated_comments}")
    print(f"  Total: {updated_inmates + updated_lookups + updated_comments}")
    print("\nAll datetime values are now timezone-aware (UTC).")


if __name__ == "__main__":
    asyncio.run(migrate_timezones())
