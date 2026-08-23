"""Bootstrap the database schema before the application starts.

Intended to run as ``python -m ibp.db_bootstrap`` (e.g. from the container
entrypoint).  It inspects the configured database and does the right thing:

- Empty database (no tables): run ``alembic upgrade head`` to build the full
  schema from the baseline migration.
- Tables exist but no ``alembic_version`` table: the database was created by
  ``Base.metadata.create_all`` (e.g. via ``scripts/migrate.py``).  If the
  schema already matches the current models, stamp it as ``head``; if it
  predates the post-baseline migrations, stamp it at the baseline and upgrade.
- Otherwise: run ``alembic upgrade head`` to apply any pending migrations.

Exits non-zero if anything goes wrong.
"""

import logging
import sys
import urllib.parse
from pathlib import Path

import sqlalchemy as sa

from alembic import command
from alembic.config import Config

from .base import config as server_config

logger = logging.getLogger(__name__)

BASELINE_REVISION = "1e01dc9942d5"

_SYNC_SCHEME_MAPPING = {
    "postgresql+asyncpg": "postgresql",
    "postgres": "postgresql",
    "sqlite+aiosqlite": "sqlite",
}


def get_sync_database_url() -> str:
    """Get the application database URL with any async driver stripped."""
    uri = server_config.get("database", "uri")
    parsed = urllib.parse.urlparse(uri)
    scheme = _SYNC_SCHEME_MAPPING.get(parsed.scheme, parsed.scheme)
    return uri.replace(f"{parsed.scheme}://", f"{scheme}://", 1)


def build_alembic_config(url: str) -> Config:
    """Build an alembic Config pointed at the project alembic.ini."""
    toplevel = Path(__file__).resolve().parent.parent
    alembic_ini = toplevel / "alembic.ini"
    if not alembic_ini.exists():
        raise FileNotFoundError(f"alembic.ini not found at {alembic_ini}")

    config = Config(str(alembic_ini))
    config.set_main_option("sqlalchemy.url", url)
    return config


def inspect_database(url: str) -> tuple[set[str], bool]:
    """Return (table names, whether requests.request_id exists)."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            has_request_id = "requests" in tables and any(
                column["name"] == "request_id"
                for column in inspector.get_columns("requests")
            )
    finally:
        engine.dispose()
    return tables, has_request_id


def bootstrap() -> None:
    """Bring the database schema up to date."""
    url = get_sync_database_url()
    tables, has_request_id = inspect_database(url)
    config = build_alembic_config(url)

    if not tables:
        logger.info("Empty database detected; running alembic upgrade to head.")
        command.upgrade(config, "head")
    elif "alembic_version" not in tables:
        if has_request_id:
            # Schema was created by Base.metadata.create_all from the current
            # models; adopt it as-is.
            logger.info(
                "Existing schema without alembic_version detected; stamping head."
            )
            command.stamp(config, "head")
        else:
            # Schema was created by create_all from pre-request_id models;
            # adopt it at the baseline and replay the later migrations.
            logger.info(
                "Existing pre-request_id schema without alembic_version detected; "
                "stamping baseline %s and upgrading to head.",
                BASELINE_REVISION,
            )
            command.stamp(config, BASELINE_REVISION)
            command.upgrade(config, "head")
    else:
        logger.info("alembic_version table present; running alembic upgrade to head.")
        command.upgrade(config, "head")

    logger.info("Database schema bootstrap complete.")


def main() -> int:
    """Run the bootstrap, logging failures and returning an exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        bootstrap()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Database schema bootstrap failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
