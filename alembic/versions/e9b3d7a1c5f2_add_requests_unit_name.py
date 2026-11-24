"""Add requests.unit_name

Revision ID: e9b3d7a1c5f2
Revises: a3c8e6f2d9b4
Create Date: 2026-11-24 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9b3d7a1c5f2'
down_revision: Union[str, None] = 'a3c8e6f2d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nullable with no default and no data step: the unit an inmate was at
    when an existing request was filled is not known, and copying today's
    unit would record a transfer's new unit as if it were the old one.
    """
    op.add_column('requests', sa.Column('unit_name', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('requests') as batch_op:
        batch_op.drop_column('unit_name')
