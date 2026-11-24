"""Add requests.unit_name and requests.unit_recorded

Revision ID: e9b3d7a1c5f2
Revises: a3c8e6f2d9b4
Create Date: 2026-11-24 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9b3d7a1c5f2'
down_revision: Union[str, None] = 'a3c8e6f2d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def new_columns() -> list[sa.Column]:
    """The columns this revision adds, in order."""
    return [
        sa.Column('unit_name', sa.String(), nullable=True),
        sa.Column(
            'unit_recorded', sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    ]


def check_existing(found: dict, column: sa.Column) -> None:
    """Fail unless a column already there is the one this revision adds."""
    if not isinstance(found['type'], type(column.type)) or (
        bool(found['nullable']) != bool(column.nullable)
    ):
        raise RuntimeError(
            f"requests.{column.name} exists but is not "
            f"{column.type}{'' if column.nullable else ' NOT NULL'}: "
            f"found {found['type']}{'' if found['nullable'] else ' NOT NULL'}"
        )


def upgrade() -> None:
    """Upgrade schema.

    No data step: the unit an inmate was at when an existing request was
    filled is not known, so every existing request records none
    (``unit_recorded`` false, ``unit_name`` NULL); copying today's unit
    would record a transfer's new unit as if it were the old one.

    Each column is added only if it is missing: SQLite DDL isn't
    transactional under alembic, so a run interrupted after adding one may
    have left it without recording this revision. A column already there
    must have the type and nullability this revision gives it.
    """
    existing = (
        {}
        if context.is_offline_mode()
        else {
            found['name']: found
            for found in sa.inspect(op.get_bind()).get_columns('requests')
        }
    )
    for column in new_columns():
        if column.name in existing:
            check_existing(existing[column.name], column)
        else:
            op.add_column('requests', column)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('requests') as batch_op:
        batch_op.drop_column('unit_recorded')
        batch_op.drop_column('unit_name')
