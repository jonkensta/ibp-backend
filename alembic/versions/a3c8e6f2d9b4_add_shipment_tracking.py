"""Add shipments, tracking_events and package_shipments

Revision ID: a3c8e6f2d9b4
Revises: d2a6f8c3e1b7
Create Date: 2026-11-23 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3c8e6f2d9b4'
down_revision: Union[str, None] = 'd2a6f8c3e1b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def jurisdiction_type() -> sa.types.TypeEngine:
    """The existing jurisdiction_enum type, never created or dropped here."""
    return sa.Enum('Texas', 'Federal', name='jurisdiction_enum').with_variant(
        postgresql.ENUM('Texas', 'Federal', name='jurisdiction_enum', create_type=False),
        'postgresql',
    )


def upgrade() -> None:
    """Upgrade schema.

    New tables only, filled by ``python -m ibp.shipment_sync``; nothing is
    backfilled here.
    """
    op.create_table(
        'shipments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('easypost_id', sa.String(length=40), nullable=False),
        sa.Column('tracking_code', sa.String(length=64), nullable=False),
        sa.Column('tracker_id', sa.String(length=40), nullable=True),
        # TZDateTime is a TypeDecorator over DateTime(timezone=True)
        sa.Column('purchased_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('weight_oz', sa.Float(), nullable=True),
        sa.Column('cost_cents', sa.Integer(), nullable=True),
        sa.Column('service', sa.String(length=40), nullable=True),
        sa.Column('reference', sa.String(length=255), nullable=True),
        sa.Column(
            'kind',
            sa.Enum(
                'individual', 'bulk', 'other',
                name='shipment_kind_enum', native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column('unit_jurisdiction', jurisdiction_type(), nullable=True),
        sa.Column('unit_name', sa.String(), nullable=True),
        sa.Column('request_jurisdiction', jurisdiction_type(), nullable=True),
        sa.Column('request_inmate_id', sa.Integer(), nullable=True),
        sa.Column('request_index', sa.Integer(), nullable=True),
        sa.Column('inmate_number', sa.String(length=8), nullable=True),
        sa.Column('refund_status', sa.String(length=20), nullable=True),
        sa.Column('tracker_status', sa.String(length=30), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_shipments')),
        sa.UniqueConstraint('easypost_id', name=op.f('uq_shipments_easypost_id')),
    )
    op.create_index(
        op.f('ix_shipments_tracking_code'), 'shipments', ['tracking_code']
    )

    op.create_table(
        'tracking_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('shipment_id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('local_time', sa.String(length=40), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=True),
        sa.Column('status_detail', sa.String(length=40), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('city', sa.String(length=100), nullable=True),
        sa.Column('state', sa.String(length=20), nullable=True),
        sa.Column('zip', sa.String(length=12), nullable=True),
        sa.Column(
            'occurrence', sa.Integer(), server_default=sa.text('0'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['shipment_id'],
            ['shipments.id'],
            name=op.f('fk_tracking_events_shipment_id_shipments'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_tracking_events')),
    )
    op.create_index(
        op.f('ix_tracking_events_shipment_id'), 'tracking_events', ['shipment_id']
    )
    # NULLs are distinct in a unique index, so optional values compare as ''.
    op.create_index(
        'uq_tracking_events_event',
        'tracking_events',
        [
            sa.text('shipment_id'),
            sa.text('occurred_at'),
            sa.text("coalesce(status, '')"),
            sa.text("coalesce(status_detail, '')"),
            sa.text("coalesce(message, '')"),
            sa.text("coalesce(city, '')"),
            sa.text("coalesce(state, '')"),
            sa.text("coalesce(zip, '')"),
            sa.text('occurrence'),
        ],
        unique=True,
    )

    op.create_table(
        'package_shipments',
        sa.Column('inmate_jurisdiction', jurisdiction_type(), nullable=False),
        sa.Column('inmate_id', sa.Integer(), nullable=False),
        sa.Column('request_index', sa.Integer(), nullable=False),
        sa.Column('shipment_id', sa.Integer(), nullable=False),
        sa.Column(
            'basis',
            sa.Enum(
                'reference', 'inmate_number', 'unit_session',
                name='link_basis_enum', native_enum=False,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['inmate_jurisdiction', 'inmate_id', 'request_index'],
            ['requests.inmate_jurisdiction', 'requests.inmate_id', 'requests.index'],
            name=op.f('fk_package_shipments_inmate_jurisdiction_requests'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['shipment_id'],
            ['shipments.id'],
            name=op.f('fk_package_shipments_shipment_id_shipments'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'inmate_jurisdiction', 'inmate_id', 'request_index', 'shipment_id',
            name=op.f('pk_package_shipments'),
        ),
    )
    op.create_index(
        'ix_package_shipments_shipment', 'package_shipments', ['shipment_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_package_shipments_shipment', table_name='package_shipments')
    op.drop_table('package_shipments')
    op.drop_index('uq_tracking_events_event', table_name='tracking_events')
    op.drop_index(op.f('ix_tracking_events_shipment_id'), table_name='tracking_events')
    op.drop_table('tracking_events')
    op.drop_index(op.f('ix_shipments_tracking_code'), table_name='shipments')
    op.drop_table('shipments')
