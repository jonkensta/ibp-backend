"""Add package items, registrations and requests.contents_revision

Revision ID: f7a3c9d1b2e6
Revises: e5d2b8f4c7a1
Create Date: 2026-11-05 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f7a3c9d1b2e6'
down_revision: Union[str, None] = 'e5d2b8f4c7a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def jurisdiction_type() -> sa.types.TypeEngine:
    """The existing jurisdiction_enum type, never created or dropped here."""
    return sa.Enum('Texas', 'Federal', name='jurisdiction_enum').with_variant(
        postgresql.ENUM('Texas', 'Federal', name='jurisdiction_enum', create_type=False),
        'postgresql',
    )


def request_key_columns() -> list[sa.Column]:
    """The columns referencing a request's composite key."""
    return [
        sa.Column('inmate_jurisdiction', jurisdiction_type(), nullable=False),
        sa.Column('inmate_id', sa.Integer(), nullable=False),
        sa.Column('request_index', sa.Integer(), nullable=False),
    ]


def request_key_fk(table: str) -> sa.ForeignKeyConstraint:
    """The composite foreign key to requests, named as create_all names it."""
    return sa.ForeignKeyConstraint(
        ['inmate_jurisdiction', 'inmate_id', 'request_index'],
        ['requests.inmate_jurisdiction', 'requests.inmate_id', 'requests.index'],
        name=op.f(f'fk_{table}_inmate_jurisdiction_requests'),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'requests',
        sa.Column(
            'contents_revision',
            sa.Integer(),
            server_default=sa.text('0'),
            nullable=False,
        ),
    )

    op.create_table(
        'package_items',
        sa.Column('id', sa.Integer(), nullable=False),
        *request_key_columns(),
        sa.Column('revision', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('isbn', sa.String(length=13), nullable=True),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('author', sa.String(length=100), nullable=True),
        sa.Column(
            'source',
            sa.Enum('isbn', 'manual', name='item_source_enum', native_enum=False),
            nullable=False,
        ),
        # TZDateTime is a TypeDecorator over DateTime(timezone=True)
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
        request_key_fk('package_items'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_package_items')),
        sqlite_autoincrement=True,
    )
    op.create_index(
        'ix_package_items_request',
        'package_items',
        ['inmate_jurisdiction', 'inmate_id', 'request_index'],
    )

    op.create_table(
        'registrations',
        sa.Column('id', sa.Integer(), nullable=False),
        # Kept after the item is deleted, so no foreign key.
        sa.Column('item_id', sa.Integer(), nullable=False),
        *request_key_columns(),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('author', sa.String(length=100), nullable=True),
        sa.Column('tdcj_number', sa.String(length=8), nullable=False),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
        request_key_fk('registrations'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_registrations')),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f('ix_registrations_item_id'), 'registrations', ['item_id']
    )
    op.create_index(
        'ix_registrations_request',
        'registrations',
        ['inmate_jurisdiction', 'inmate_id', 'request_index'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_registrations_request', table_name='registrations')
    op.drop_index(op.f('ix_registrations_item_id'), table_name='registrations')
    op.drop_table('registrations')
    op.drop_index('ix_package_items_request', table_name='package_items')
    op.drop_table('package_items')
    with op.batch_alter_table('requests') as batch:
        batch.drop_column('contents_revision')
