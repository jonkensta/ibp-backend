"""Bootstrap the database schema before the application starts.

Intended to run as ``python -m ibp.db_bootstrap`` (e.g. from the container
entrypoint).  It inspects the configured database and does the right thing:

- Empty database (no tables): run ``alembic upgrade head`` to build the full
  schema from the baseline migration.
- Tables exist but no ``alembic_version`` table: the database was created by
  ``Base.metadata.create_all`` (e.g. via ``scripts/migrate.py``).  Stamp the
  newest schema revision it matches and upgrade to ``head``, which also runs
  any later data-only migrations (they are safe to run on any data).
- Otherwise: run ``alembic upgrade head`` to apply any pending migrations.

Then the schema is checked to have every column of the models: one a
half-run migration left missing at a recorded revision is restored by
replaying that revision (``check_columns``); any other is an error.

Finally, the single ``shipping_settings`` row is seeded with the production
defaults if it is missing (``create_all`` builds the table but, unlike the
migration, does not seed it).

Exits non-zero if anything goes wrong.
"""

import logging
import sys
import urllib.parse
from pathlib import Path

import sqlalchemy as sa

from alembic import command
from alembic.config import Config

from . import models
from .shipment_links import UNSCANNED
from .base import config as server_config

logger = logging.getLogger(__name__)

BASELINE_REVISION = "1e01dc9942d5"
# Last revision before the shipping_settings table was added.
PRE_SHIPPING_SETTINGS_REVISION = "a7c31f9e5b24"
# Last revision before the books table was added.
PRE_BOOKS_REVISION = "b5e2d8c4a1f3"
# Last revision before inmates.next_request_index was added.
PRE_NEXT_REQUEST_INDEX_REVISION = "c3f1a9d27e64"
# Last revision before the package_items table was added.
PRE_PACKAGE_ITEMS_REVISION = "e5d2b8f4c7a1"
# Last revision before the upcs table was added.
PRE_UPCS_REVISION = "f7a3c9d1b2e6"
# Last revision before the confirmation_number columns were added.
PRE_CONFIRMATION_NUMBER_REVISION = "a9d4e2c6b8f1"
# Last revision before the inmates.pronouns columns were added.
PRE_PRONOUNS_REVISION = "b8e1f4a2c9d7"
# Last revision before Texas race and sex codes were rewritten as words. That
# migration adds no column, so a schema with every column of the current
# models is adopted here and upgraded, rewriting any codes it holds (e.g.
# legacy ones from scripts/migrate.py); stamping it head would skip that.
PRE_RACE_SEX_WORDS_REVISION = "c4e7a2f9d1b3"
# Last revision before the shipments, tracking_events and package_shipments
# tables were added; it is the race and sex data migration.
PRE_SHIPMENTS_REVISION = "d2a6f8c3e1b7"
# Last revision before requests.unit_name and unit_recorded were added; it
# adds the shipments tables.
PRE_REQUEST_UNIT_REVISION = "a3c8e6f2d9b4"
# Last revision before shipments.ever_scanned was added; it adds the request
# unit columns.
PRE_EVER_SCANNED_REVISION = "e9b3d7a1c5f2"

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


def inspect_database(url: str) -> tuple[set[str], set[str]]:
    """Return (table names, columns as ``table.column``) of the database."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            columns = {
                f"{table}.{column['name']}"
                for table in tables
                for column in inspector.get_columns(table)
            }
    finally:
        engine.dispose()
    return tables, columns


def seed_shipping_settings(url: str) -> None:
    """Insert the default shipping settings row if it is missing."""
    table = models.Base.metadata.tables[models.ShippingSettings.__tablename__]
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            exists = connection.execute(
                sa.select(table.c.id).where(table.c.id == models.SHIPPING_SETTINGS_ID)
            ).first()
            if exists is None:
                logger.info("Seeding default shipping settings.")
                connection.execute(
                    sa.insert(table).values(
                        id=models.SHIPPING_SETTINGS_ID,
                        **models.DEFAULT_SHIPPING_SETTINGS,
                    )
                )
    finally:
        engine.dispose()


def backfill_ever_scanned(url: str) -> int:
    """Set ``shipments.ever_scanned`` for every label whose stored status or
    events show a scan (the ``f4c2a8e6b1d3`` backfill); return how many."""
    unscanned: sa.BindParameter = sa.bindparam(
        "unscanned", sorted(UNSCANNED), expanding=True
    )
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            return connection.execute(
                sa.text(
                    "UPDATE shipments SET ever_scanned = :yes WHERE NOT ever_scanned "
                    "AND ((tracker_status IS NOT NULL AND tracker_status NOT IN "
                    ":unscanned) OR EXISTS (SELECT 1 FROM tracking_events AS e "
                    "WHERE e.shipment_id = shipments.id AND e.status IS NOT NULL "
                    "AND e.status NOT IN :unscanned))"
                ).bindparams(sa.bindparam("yes", True), unscanned)
            ).rowcount
    finally:
        engine.dispose()


def built_revision(columns: set[str]) -> str:
    """The newest revision a create_all schema with the shipment tables has
    every column of."""
    if "shipments.ever_scanned" in columns:
        return "head"
    if "requests.unit_recorded" in columns:
        return PRE_EVER_SCANNED_REVISION
    return PRE_REQUEST_UNIT_REVISION


def adopt_create_all_schema(
    config: Config, tables: set[str], columns: set[str]
) -> None:
    """Stamp a schema built by ``create_all`` (no ``alembic_version``) and
    upgrade it to head.

    The schema is stamped at the newest revision whose tables and columns it
    has, then upgraded, which also runs every later data migration.
    """
    if "shipments" in tables:
        # Schema was created by Base.metadata.create_all from the current
        # models (with shipments.ever_scanned) or from older ones. Adopt it
        # before the race and sex data migration and run that, then stamp
        # the schema migrations after it that create_all already built, and
        # replay the rest (which also backfills ever_scanned).
        built = built_revision(columns)
        logger.info(
            "Existing schema without alembic_version detected; stamping %s, "
            "upgrading to %s, then stamping %s and upgrading to head.",
            PRE_RACE_SEX_WORDS_REVISION,
            PRE_SHIPMENTS_REVISION,
            built,
        )
        command.stamp(config, PRE_RACE_SEX_WORDS_REVISION)
        command.upgrade(config, PRE_SHIPMENTS_REVISION)
        command.stamp(config, built)
        command.upgrade(config, "head")
    elif "inmates.pronouns" in columns:
        # Schema was created by create_all from pre-shipments models;
        # adopt it at the last schema revision before the data migration
        # and replay the later migrations.
        logger.info(
            "Existing pre-shipments schema without alembic_version detected; "
            "stamping %s and upgrading to head.",
            PRE_RACE_SEX_WORDS_REVISION,
        )
        command.stamp(config, PRE_RACE_SEX_WORDS_REVISION)
        command.upgrade(config, "head")
    elif "registrations.confirmation_number" in columns:
        # Schema was created by create_all from pre-pronouns models; adopt
        # it there and replay the later migrations.
        logger.info(
            "Existing pre-pronouns schema without alembic_version detected; "
            "stamping %s and upgrading to head.",
            PRE_PRONOUNS_REVISION,
        )
        command.stamp(config, PRE_PRONOUNS_REVISION)
        command.upgrade(config, "head")
    elif "upcs" in tables:
        # Schema was created by create_all from pre-confirmation_number
        # models; adopt it there and replay the later migrations.
        logger.info(
            "Existing pre-confirmation_number schema without alembic_version "
            "detected; stamping %s and upgrading to head.",
            PRE_CONFIRMATION_NUMBER_REVISION,
        )
        command.stamp(config, PRE_CONFIRMATION_NUMBER_REVISION)
        command.upgrade(config, "head")
    elif "package_items" in tables:
        # Schema was created by create_all from pre-upcs models; adopt it
        # there and replay the later migrations.
        logger.info(
            "Existing pre-upcs schema without alembic_version detected; "
            "stamping %s and upgrading to head.",
            PRE_UPCS_REVISION,
        )
        command.stamp(config, PRE_UPCS_REVISION)
        command.upgrade(config, "head")
    elif "inmates.next_request_index" in columns:
        # Schema was created by create_all from pre-package_items models;
        # adopt it there and replay the later migrations.
        logger.info(
            "Existing pre-package_items schema without alembic_version "
            "detected; stamping %s and upgrading to head.",
            PRE_PACKAGE_ITEMS_REVISION,
        )
        command.stamp(config, PRE_PACKAGE_ITEMS_REVISION)
        command.upgrade(config, "head")
    elif "books" in tables:
        # Schema was created by create_all from pre-next_request_index
        # models; adopt it there and replay the later migrations.
        logger.info(
            "Existing pre-next_request_index schema without alembic_version "
            "detected; stamping %s and upgrading to head.",
            PRE_NEXT_REQUEST_INDEX_REVISION,
        )
        command.stamp(config, PRE_NEXT_REQUEST_INDEX_REVISION)
        command.upgrade(config, "head")
    elif "shipping_settings" in tables:
        # Schema was created by create_all from pre-books models; adopt it
        # there and replay the later migrations.
        logger.info(
            "Existing pre-books schema without alembic_version detected; "
            "stamping %s and upgrading to head.",
            PRE_BOOKS_REVISION,
        )
        command.stamp(config, PRE_BOOKS_REVISION)
        command.upgrade(config, "head")
    elif "requests.request_id" in columns:
        # Schema was created by create_all from pre-shipping_settings
        # models; adopt it there and replay the later migrations.
        logger.info(
            "Existing pre-shipping_settings schema without alembic_version "
            "detected; stamping %s and upgrading to head.",
            PRE_SHIPPING_SETTINGS_REVISION,
        )
        command.stamp(config, PRE_SHIPPING_SETTINGS_REVISION)
        command.upgrade(config, "head")
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


# Columns a revision adds that a run interrupted partway (SQLite DDL isn't
# transactional under alembic) can leave missing at a recorded revision
# that expects them, with the revision to replay from to restore them.
REPAIRABLE_COLUMNS = {
    "requests.unit_name": PRE_REQUEST_UNIT_REVISION,
    "requests.unit_recorded": PRE_REQUEST_UNIT_REVISION,
    "shipments.ever_scanned": PRE_EVER_SCANNED_REVISION,
}
# Those revisions, oldest first: replaying from the oldest one needed
# replays the later ones too, each adding only what is missing.
REPAIR_ORDER = [PRE_REQUEST_UNIT_REVISION, PRE_EVER_SCANNED_REVISION]


def missing_columns(url: str) -> set[str]:
    """Columns of the current models the database lacks (``table.column``)."""
    _, columns = inspect_database(url)
    expected = {
        f"{table.name}.{column.name}"
        for table in models.Base.metadata.sorted_tables
        for column in table.columns
    }
    return expected - columns


def refuse_lost_scans(url: str) -> None:
    """Fail if ``shipments.ever_scanned`` went missing with labels stored.

    The flag keeps scans that a later status replaced, so it can't be
    rebuilt from what is stored: replaying its backfill would drop them and
    the links they are the evidence for. An empty ``shipments`` loses
    nothing, so the column is restored then.
    """
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            stored = connection.execute(
                sa.text("SELECT COUNT(*) FROM shipments")
            ).scalar_one()
    finally:
        engine.dispose()
    if stored:
        raise RuntimeError(
            f"shipments.ever_scanned is missing at head with {stored} labels "
            "stored: which of them USPS ever scanned can't be rebuilt from what "
            "is stored; restore the database from the backup taken before the "
            "upgrade (deploy/pi/README.md, roll back)"
        )


def check_columns(config: Config, url: str) -> None:
    """Make sure the upgraded schema has every column of the models.

    A column a half-run upgrade or downgrade left missing is restored by
    replaying its revision, whose upgrade adds only what is missing, except
    ``shipments.ever_scanned`` with labels stored (``refuse_lost_scans``);
    any other missing column fails the bootstrap loudly.
    """
    missing = missing_columns(url)
    if "shipments.ever_scanned" in missing:
        refuse_lost_scans(url)
    if missing and missing <= REPAIRABLE_COLUMNS.keys():
        needed = {REPAIRABLE_COLUMNS[column] for column in missing}
        revision = next(rev for rev in REPAIR_ORDER if rev in needed)
        logger.warning(
            "Schema at head lacks %s; replaying from %s to restore it.",
            ", ".join(sorted(missing)),
            revision,
        )
        command.stamp(config, revision)
        command.upgrade(config, "head")
        missing = missing_columns(url)
    if missing:
        raise RuntimeError(
            "schema at head lacks columns: " + ", ".join(sorted(missing))
        )


def bootstrap() -> None:
    """Bring the database schema up to date."""
    url = get_sync_database_url()
    tables, columns = inspect_database(url)
    config = build_alembic_config(url)

    if not tables:
        logger.info("Empty database detected; running alembic upgrade to head.")
        command.upgrade(config, "head")
    elif "alembic_version" not in tables:
        adopt_create_all_schema(config, tables, columns)
        # A schema stamped past the ever_scanned revision missed its backfill.
        if "shipments" in tables:
            backfill_ever_scanned(url)
    else:
        logger.info("alembic_version table present; running alembic upgrade to head.")
        command.upgrade(config, "head")

    check_columns(config, url)
    seed_shipping_settings(url)
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
