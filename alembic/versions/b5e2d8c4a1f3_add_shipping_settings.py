"""Add shipping settings

Revision ID: b5e2d8c4a1f3
Revises: a7c31f9e5b24
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e2d8c4a1f3'
down_revision: Union[str, None] = 'a7c31f9e5b24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    table = op.create_table(
        'shipping_settings',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('return_name', sa.String(length=100), nullable=False),
        sa.Column('return_street1', sa.String(length=100), nullable=False),
        sa.Column('return_street2', sa.String(length=100), nullable=True),
        sa.Column('return_city', sa.String(length=100), nullable=False),
        sa.Column('return_state', sa.String(length=2), nullable=False),
        sa.Column('return_zipcode', sa.String(length=10), nullable=False),
        sa.Column('unit_recipient_name', sa.String(length=100), nullable=False),
        sa.CheckConstraint('id = 1', name=op.f('ck_shipping_settings_single_row')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_shipping_settings')),
    )

    # Seed the production values (legacy server.conf [address] and
    # [shipping]), frozen here rather than imported from the app.
    op.bulk_insert(
        table,
        [
            {
                'id': 1,
                'return_name': 'Inside Books Project',
                'return_street1': 'PO Box 301029',
                'return_street2': '12 ST BOOKS',
                'return_city': 'Austin',
                'return_state': 'TX',
                'return_zipcode': '78703',
                'unit_recipient_name': 'ATTN: MAILROOM STAFF',
            }
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('shipping_settings')
