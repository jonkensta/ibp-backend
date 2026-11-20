"""Add portal confirmation numbers to package_items and registrations

Revision ID: b8e1f4a2c9d7
Revises: a9d4e2c6b8f1
Create Date: 2026-11-19 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8e1f4a2c9d7'
down_revision: Union[str, None] = 'a9d4e2c6b8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('package_items', 'registrations')


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        op.add_column(
            table,
            sa.Column('confirmation_number', sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('confirmation_number')
