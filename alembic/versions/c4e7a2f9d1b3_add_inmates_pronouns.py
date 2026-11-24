"""Add inmates.pronouns, pronouns_set_by and pronouns_set_at

Revision ID: c4e7a2f9d1b3
Revises: b8e1f4a2c9d7
Create Date: 2026-11-23 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e7a2f9d1b3'
down_revision: Union[str, None] = 'b8e1f4a2c9d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ('pronouns', 'pronouns_set_by', 'pronouns_set_at')


def upgrade() -> None:
    """Upgrade schema.

    Nullable with no default and no data step: an inmate's pronouns are only
    ever recorded from a letter, so every existing inmate starts with none.
    """
    op.add_column(
        'inmates', sa.Column('pronouns', sa.String(length=30), nullable=True)
    )
    op.add_column(
        'inmates', sa.Column('pronouns_set_by', sa.String(length=40), nullable=True)
    )
    # TZDateTime is a TypeDecorator over DateTime(timezone=True)
    op.add_column(
        'inmates',
        sa.Column('pronouns_set_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    On SQLite the columns are dropped with a native ``ALTER TABLE ... DROP
    COLUMN``, not batch mode: rebuilding ``inmates`` would drop the table,
    and with foreign keys on, that deletes (or fails on) the rows of every
    table referring to it. Each is dropped only if it is there, so an
    interrupted downgrade can be run again.
    """
    existing = (
        set(COLUMNS)
        if context.is_offline_mode()
        else {found['name'] for found in sa.inspect(op.get_bind()).get_columns('inmates')}
    )
    for column in COLUMNS:
        if column not in existing:
            continue
        if op.get_context().dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE inmates DROP COLUMN {column}')
        else:
            op.drop_column('inmates', column)
