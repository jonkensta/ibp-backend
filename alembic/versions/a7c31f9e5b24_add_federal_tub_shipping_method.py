"""Add Federal Tub shipping method

Revision ID: a7c31f9e5b24
Revises: d86e928e232a
Create Date: 2026-08-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c31f9e5b24'
down_revision: Union[str, None] = 'd86e928e232a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_ENUM = sa.Enum("Box", "Individual", name="shipping_enum")
NEW_ENUM = sa.Enum("Box", "Individual", "Federal Tub", name="shipping_enum")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # The schema on postgres is created via Base.metadata.create_all,
        # which makes shipping_enum a native ENUM type.
        op.execute("ALTER TYPE shipping_enum ADD VALUE IF NOT EXISTS 'Federal Tub'")
        return

    # On sqlite the Enum renders as VARCHAR (no CHECK constraint is emitted by
    # SQLAlchemy 2.x by default), so widen the column from VARCHAR(10) to
    # VARCHAR(11) to fit 'Federal Tub'.
    with op.batch_alter_table("units") as batch:
        batch.alter_column(
            "shipping_method",
            existing_type=OLD_ENUM,
            type_=NEW_ENUM,
            existing_nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM units WHERE shipping_method = :v"),
        {"v": "Federal Tub"},
    ).scalar()
    if count:
        raise RuntimeError(
            f"Cannot downgrade: {count} unit(s) still use 'Federal Tub'."
        )

    if bind.dialect.name == "postgresql":
        # Postgres cannot remove a value from an enum type; recreate it.
        op.execute("ALTER TYPE shipping_enum RENAME TO shipping_enum_old")
        op.execute("CREATE TYPE shipping_enum AS ENUM ('Box', 'Individual')")
        op.execute(
            "ALTER TABLE units ALTER COLUMN shipping_method "
            "TYPE shipping_enum USING shipping_method::text::shipping_enum"
        )
        op.execute("DROP TYPE shipping_enum_old")
        return

    with op.batch_alter_table("units") as batch:
        batch.alter_column(
            "shipping_method",
            existing_type=NEW_ENUM,
            type_=OLD_ENUM,
            existing_nullable=True,
        )
