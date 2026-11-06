"""Add inmates.next_request_index (stable request indexes)

Revision ID: e5d2b8f4c7a1
Revises: c3f1a9d27e64
Create Date: 2026-11-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5d2b8f4c7a1'
down_revision: Union[str, None] = 'c3f1a9d27e64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'inmates',
        sa.Column(
            'next_request_index',
            sa.Integer(),
            server_default=sa.text('0'),
            nullable=False,
        ),
    )

    # Continue each inmate's numbering after its highest existing index, so
    # new requests never take an index a printed label may still carry.
    inmates = sa.table(
        'inmates',
        sa.column('jurisdiction'),
        sa.column('id'),
        sa.column('next_request_index'),
    )
    requests = sa.table(
        'requests',
        sa.column('inmate_jurisdiction'),
        sa.column('inmate_id'),
        sa.column('index'),
    )
    next_index = (
        sa.select(sa.func.max(requests.c.index) + 1)
        .where(
            requests.c.inmate_jurisdiction == inmates.c.jurisdiction,
            requests.c.inmate_id == inmates.c.id,
        )
        .scalar_subquery()
    )
    op.execute(
        inmates.update().values(next_request_index=sa.func.coalesce(next_index, 0))
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('inmates') as batch:
        batch.drop_column('next_request_index')
