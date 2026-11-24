"""Add shipments.ever_scanned

Revision ID: f4c2a8e6b1d3
Revises: e9b3d7a1c5f2
Create Date: 2026-11-24 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f4c2a8e6b1d3'
down_revision: Union[str, None] = 'e9b3d7a1c5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# shipment_links.UNSCANNED as of this revision: statuses that are no
# evidence USPS handled a package.
UNSCANNED = ('unknown', 'pre_transit', 'error', 'cancelled')


def ever_scanned_column() -> sa.Column:
    """The column this revision adds."""
    return sa.Column(
        'ever_scanned', sa.Boolean(), server_default=sa.false(), nullable=False
    )


def existing_columns() -> dict[str, dict]:
    """The columns ``shipments`` has now (none known in offline mode)."""
    if context.is_offline_mode():
        return {}
    return {
        found['name']: found
        for found in sa.inspect(op.get_bind()).get_columns('shipments')
    }


def upgrade() -> None:
    """Upgrade schema.

    The column is added only if it is missing (one already there must be a
    NOT NULL boolean), then set for every label the stored data shows USPS
    handled: a tracker status or any event status that is not in
    ``UNSCANNED``. That data is all there is to it, so replaying this
    revision fully restores the column (``db_bootstrap`` does when it is
    missing at head); it only ever sets the flag, never clears it.
    """
    column = ever_scanned_column()
    found = existing_columns().get(column.name)
    if found is None:
        op.add_column('shipments', column)
    elif not isinstance(found['type'], sa.Boolean) or found['nullable']:
        raise RuntimeError(
            f"shipments.ever_scanned exists but is not BOOLEAN NOT NULL: found "
            f"{found['type']}{'' if found['nullable'] else ' NOT NULL'}"
        )
    unscanned = sa.bindparam('unscanned', list(UNSCANNED), expanding=True)
    op.execute(
        sa.text(
            'UPDATE shipments SET ever_scanned = :yes WHERE NOT ever_scanned AND ('
            '(tracker_status IS NOT NULL AND tracker_status NOT IN :unscanned) '
            'OR EXISTS (SELECT 1 FROM tracking_events AS e '
            'WHERE e.shipment_id = shipments.id AND e.status IS NOT NULL '
            'AND e.status NOT IN :unscanned))'
        ).bindparams(sa.bindparam('yes', True), unscanned)
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropped natively on SQLite (no batch rebuild of ``shipments``, whose
    cascade would empty ``tracking_events`` and ``package_shipments`` with
    foreign keys on), and only if it is there.
    """
    found = existing_columns()
    if found and 'ever_scanned' not in found:
        return
    if op.get_context().dialect.name == 'sqlite':
        op.execute('ALTER TABLE shipments DROP COLUMN ever_scanned')
    else:
        op.drop_column('shipments', 'ever_scanned')
