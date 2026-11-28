import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, event
from sqlalchemy import pool

from alembic import context

# Import application models for autogenerate support
from ibp.db import use_sqlite_pragmas
from ibp.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate support
target_metadata = Base.metadata

# Configure database URL from environment or default
# Convert async drivers to sync for alembic
def get_database_url():
    """Get database URL, converting async drivers to sync versions."""
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data.db")

    # Convert async drivers to sync for alembic
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    db_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")

    return db_url

# Override sqlalchemy.url in config if not already set
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_database_url())

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # WAL and a busy timeout, as for the app's own connections.
    use_sqlite_pragmas(connectable)
    sqlite = connectable.dialect.name == "sqlite"
    if sqlite:
        # SQLite's DDL is transactional, but the pysqlite driver only opens a
        # transaction before INSERT/UPDATE/DELETE, so each ALTER TABLE would
        # commit on its own and an interrupted migration could leave its
        # columns half changed at the old revision. Take over the BEGIN, and
        # run the whole upgrade or downgrade, revision updates included, as
        # one transaction.
        @event.listens_for(connectable, "connect")
        def _no_driver_transactions(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(connectable, "begin")
        def _begin(connection):
            connection.exec_driver_sql("BEGIN")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transactional_ddl=True if sqlite else None,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
