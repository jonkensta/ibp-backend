"""Add upcs (UPC -> ISBN lookup cache)

Revision ID: a9d4e2c6b8f1
Revises: f7a3c9d1b2e6
Create Date: 2026-11-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d4e2c6b8f1'
down_revision: Union[str, None] = 'f7a3c9d1b2e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'upcs',
        sa.Column('upc', sa.String(length=17), nullable=False),
        sa.Column('isbn', sa.String(length=13), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        # TZDateTime is a TypeDecorator over DateTime(timezone=True)
        sa.Column('looked_up_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('upc', name=op.f('pk_upcs')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('upcs')
