#!/usr/bin/env python3
"""
Migrate data from SQLite to PostgreSQL.

Usage:
    python migrate_to_postgres.py

Migrates from data.db (SQLite) to DATABASE_URL (PostgreSQL).
Set DATABASE_URL environment variable to your PostgreSQL connection string.
"""

import asyncio
import os
import sys
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Import all models to ensure they're registered
from ibp.models import Inmate, Unit, Lookup, Request, Comment
from ibp.db import Base, get_engine_kwargs


def get_sqlite_uri():
    """Get SQLite database URI.

    Migration always reads from data.db in the server directory.
    """
    server_dir = Path(__file__).parent
    data_db_path = server_dir / "data.db"
    return f"sqlite+aiosqlite:///{data_db_path.absolute()}"


def get_postgres_uri():
    """Get PostgreSQL database URI from environment.

    Reads from DATABASE_URL environment variable.
    Ensures asyncpg driver is used.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set!")
        print("Example:")
        print('  export DATABASE_URL="postgresql://user:pass@host:5432/dbname"')
        sys.exit(1)

    # Ensure it uses asyncpg driver
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)

    return database_url


async def create_tables(engine):
    """Create all tables in PostgreSQL."""
    print("Creating tables in PostgreSQL...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables created")


async def count_records(session, model):
    """Count records in a table."""
    result = await session.execute(select(sqlalchemy.func.count()).select_from(model))
    return result.scalar()


async def migrate_table(sqlite_session, postgres_session, model, model_name):
    """Migrate all records from one table."""
    print(f"\nMigrating {model_name}...")

    # Get all records from SQLite
    result = await sqlite_session.execute(select(model))
    records = result.scalars().all()

    if not records:
        print(f"  No {model_name} records to migrate")
        return 0

    print(f"  Found {len(records)} {model_name} records")

    # Insert into PostgreSQL
    for i, record in enumerate(records, 1):
        # Create a new instance with the same data
        new_record = model(**{
            c.name: getattr(record, c.name)
            for c in model.__table__.columns
        })
        postgres_session.add(new_record)

        if i % 100 == 0:
            await postgres_session.flush()
            print(f"  Migrated {i}/{len(records)}...")

    await postgres_session.flush()
    print(f"✓ Migrated {len(records)} {model_name} records")
    return len(records)


async def verify_migration(sqlite_session, postgres_session, model, model_name):
    """Verify that migration was successful."""
    sqlite_count = await count_records(sqlite_session, model)
    postgres_count = await count_records(postgres_session, model)

    if sqlite_count == postgres_count:
        print(f"✓ {model_name}: {postgres_count} records (matches SQLite)")
        return True
    else:
        print(f"✗ {model_name}: {postgres_count} records (SQLite has {sqlite_count})")
        return False


async def main():
    """Main migration function."""
    print("=" * 60)
    print("IBP Database Migration: SQLite → PostgreSQL")
    print("=" * 60)

    # Get database URIs
    sqlite_uri = get_sqlite_uri()
    postgres_uri = get_postgres_uri()

    print(f"\nSource (SQLite): {sqlite_uri}")
    print(f"Target (PostgreSQL): {postgres_uri.split('@')[0]}@***")

    # Create engines with appropriate settings
    print("\nConnecting to databases...")
    sqlite_engine = create_async_engine(sqlite_uri, **get_engine_kwargs(sqlite_uri))
    postgres_engine = create_async_engine(postgres_uri, **get_engine_kwargs(postgres_uri))

    # Create sessionmakers
    SqliteSession = async_sessionmaker(sqlite_engine, class_=AsyncSession)
    PostgresSession = async_sessionmaker(postgres_engine, class_=AsyncSession)

    try:
        # Create tables in PostgreSQL
        await create_tables(postgres_engine)

        # Start migration
        print("\n" + "=" * 60)
        print("Starting data migration...")
        print("=" * 60)

        async with SqliteSession() as sqlite_session, PostgresSession() as postgres_session:
            # Migration order matters due to foreign keys
            # 1. Units (no dependencies)
            # 2. Inmates (depends on Units)
            # 3. Lookups, Requests, Comments (depend on Inmates)

            total_migrated = 0

            # Migrate Units first
            total_migrated += await migrate_table(
                sqlite_session, postgres_session, Unit, "Units"
            )

            # Migrate Inmates
            total_migrated += await migrate_table(
                sqlite_session, postgres_session, Inmate, "Inmates"
            )

            # Migrate related tables (order doesn't matter among these)
            total_migrated += await migrate_table(
                sqlite_session, postgres_session, Lookup, "Lookups"
            )
            total_migrated += await migrate_table(
                sqlite_session, postgres_session, Request, "Requests"
            )
            total_migrated += await migrate_table(
                sqlite_session, postgres_session, Comment, "Comments"
            )

            # Commit all changes
            print("\nCommitting changes...")
            await postgres_session.commit()
            print("✓ All changes committed")

            # Verify migration
            print("\n" + "=" * 60)
            print("Verifying migration...")
            print("=" * 60)

            all_verified = True
            all_verified &= await verify_migration(
                sqlite_session, postgres_session, Unit, "Units"
            )
            all_verified &= await verify_migration(
                sqlite_session, postgres_session, Inmate, "Inmates"
            )
            all_verified &= await verify_migration(
                sqlite_session, postgres_session, Lookup, "Lookups"
            )
            all_verified &= await verify_migration(
                sqlite_session, postgres_session, Request, "Requests"
            )
            all_verified &= await verify_migration(
                sqlite_session, postgres_session, Comment, "Comments"
            )

            print("\n" + "=" * 60)
            if all_verified:
                print(f"✓ SUCCESS! Migrated {total_migrated} total records")
                print("=" * 60)
                print("\nNext steps:")
                print("1. Set DATABASE_URL in your production environment")
                print("2. Test the application with PostgreSQL")
                print("3. Deploy to Cloud Run")
            else:
                print("✗ WARNING: Some tables have mismatched record counts")
                print("=" * 60)
                print("\nPlease investigate the discrepancies before proceeding.")

    except Exception as e:
        print(f"\n✗ ERROR: Migration failed!")
        print(f"   {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        # Close connections
        await sqlite_engine.dispose()
        await postgres_engine.dispose()


if __name__ == "__main__":
    # Add missing import for sqlalchemy.func
    import sqlalchemy
    asyncio.run(main())
