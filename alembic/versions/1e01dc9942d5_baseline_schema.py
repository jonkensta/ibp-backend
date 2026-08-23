"""Baseline schema.

Creates the full schema as it existed BEFORE revision d86e928e232a, i.e. the
schema historically produced by ``Base.metadata.create_all``:

- no ``requests.request_id`` column (added by d86e928e232a)
- two-value ``shipping_enum`` ('Box', 'Individual'; widened by a7c31f9e5b24)

The tables are defined as a frozen snapshot on a standalone ``MetaData`` and
created with ``MetaData.create_all``.  This keeps the migration independent of
the (evolving) ORM models and lets SQLAlchemy handle dialect differences —
in particular, native ENUM types on PostgreSQL are created exactly once even
though ``jurisdiction_enum`` is shared by several tables (``op.create_table``
would try to CREATE TYPE once per table and fail).

Note: this migration requires a live connection ("online" mode); it does not
support ``alembic upgrade --sql`` offline generation.

Revision ID: 1e01dc9942d5
Revises:
Create Date: 2026-08-23 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1e01dc9942d5"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirror of ibp.db.Base.metadata's naming convention so that constraint names
# match databases created via Base.metadata.create_all.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _build_metadata() -> sa.MetaData:
    """Build a frozen snapshot of the pre-d86e928e232a schema."""
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    jurisdiction = sa.Enum("Texas", "Federal", name="jurisdiction_enum")
    shipping = sa.Enum("Box", "Individual", name="shipping_enum")
    action = sa.Enum("Filled", "Tossed", name="action_enum")

    sa.Table(
        "units",
        metadata,
        sa.Column("jurisdiction", jurisdiction, primary_key=True, nullable=False),
        sa.Column("name", sa.String(), primary_key=True, nullable=False),
        sa.Column("street1", sa.String(), nullable=False),
        sa.Column("street2", sa.String(), nullable=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("zipcode", sa.String(length=12), nullable=False),
        sa.Column("state", sa.String(length=3), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("shipping_method", shipping, nullable=True),
    )

    sa.Table(
        "inmates",
        metadata,
        sa.Column("jurisdiction", jurisdiction, primary_key=True, nullable=False),
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("unit_name", sa.String(), nullable=True),
        sa.Column("race", sa.String(), nullable=True),
        sa.Column("sex", sa.String(), nullable=True),
        # ReleaseDate is a TypeDecorator over String
        sa.Column("release", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        # TZDateTime is a TypeDecorator over DateTime(timezone=True)
        sa.Column("datetime_fetched", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["jurisdiction", "unit_name"],
            ["units.jurisdiction", "units.name"],
        ),
    )

    def inmate_child_table(name: str, *columns: sa.Column) -> sa.Table:
        """Build a table using the HasInmateIndex mixin layout."""
        return sa.Table(
            name,
            metadata,
            sa.Column("inmate_jurisdiction", jurisdiction, nullable=False),
            sa.Column("inmate_id", sa.Integer(), nullable=False),
            sa.Column("index", sa.Integer(), nullable=False),
            *columns,
            sa.PrimaryKeyConstraint("inmate_jurisdiction", "inmate_id", "index"),
            sa.ForeignKeyConstraint(
                ["inmate_jurisdiction", "inmate_id"],
                ["inmates.jurisdiction", "inmates.id"],
            ),
        )

    inmate_child_table(
        "lookups",
        sa.Column("datetime_created", sa.DateTime(timezone=True), nullable=False),
    )

    # NOTE: no request_id column here; it is added by revision d86e928e232a.
    inmate_child_table(
        "requests",
        sa.Column("date_processed", sa.Date(), nullable=False),
        sa.Column("date_postmarked", sa.Date(), nullable=False),
        sa.Column("action", action, nullable=False),
    )

    inmate_child_table(
        "comments",
        sa.Column("datetime_created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )

    return metadata


def upgrade() -> None:
    """Create the baseline schema."""
    _build_metadata().create_all(bind=op.get_bind())


def downgrade() -> None:
    """Drop the baseline schema."""
    _build_metadata().drop_all(bind=op.get_bind())
