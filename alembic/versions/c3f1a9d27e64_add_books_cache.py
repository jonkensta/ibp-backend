"""Add books (ISBN lookup cache)

Revision ID: c3f1a9d27e64
Revises: b5e2d8c4a1f3
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f1a9d27e64'
down_revision: Union[str, None] = 'b5e2d8c4a1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'books',
        sa.Column('isbn', sa.String(length=13), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('author', sa.String(length=255), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('confidence', sa.String(length=10), nullable=True),
        sa.Column('candidates', sa.Text(), nullable=True),
        # TZDateTime is a TypeDecorator over DateTime(timezone=True)
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('isbn', name=op.f('pk_books')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('books')
