"""Add requests.unit_name and requests.unit_recorded

Revision ID: e9b3d7a1c5f2
Revises: a3c8e6f2d9b4
Create Date: 2026-11-24 00:00:00.000000

"""

from typing import Sequence, Union

import logging

from alembic import context, op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9b3d7a1c5f2'
down_revision: Union[str, None] = 'a3c8e6f2d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger('alembic.runtime.migration')


def new_columns() -> list[sa.Column]:
    """The columns this revision adds, in order."""
    return [
        sa.Column('unit_name', sa.String(), nullable=True),
        sa.Column(
            'unit_recorded', sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    ]


def check_existing(found: dict, column: sa.Column) -> None:
    """Fail unless a column already there is the one this revision adds."""
    if not isinstance(found['type'], type(column.type)) or (
        bool(found['nullable']) != bool(column.nullable)
    ):
        raise RuntimeError(
            f"requests.{column.name} exists but is not "
            f"{column.type}{'' if column.nullable else ' NOT NULL'}: "
            f"found {found['type']}{'' if found['nullable'] else ' NOT NULL'}"
        )


def existing_columns() -> set[str]:
    """The columns ``requests`` has now (none known in offline mode)."""
    if context.is_offline_mode():
        return set()
    return {found['name'] for found in sa.inspect(op.get_bind()).get_columns('requests')}


def upgrade() -> None:
    """Upgrade schema.

    No data step: the unit an inmate was at when an existing request was
    filled is not known, so every existing request records none
    (``unit_recorded`` false, ``unit_name`` NULL); copying today's unit
    would record a transfer's new unit as if it were the old one.

    Each column is added only if it is missing: before migrations ran in
    one transaction on SQLite (``alembic/env.py``), an interrupted run could
    leave one without recording this revision. A column already there must
    have the type and nullability this revision gives it.

    Repairing such a half state keeps what history is left (``db_bootstrap``
    replays this revision when a column is missing at head):

    - ``unit_name`` there, ``unit_recorded`` missing (a downgrade stopped
      after dropping it): every request with a unit name provably recorded
      one, so it is restored as recorded; a request that recorded an
      unknown unit can't be told from an older one and is restored as not
      recorded, which is logged as a warning;
    - ``unit_recorded`` there with recorded requests, ``unit_name`` missing:
      the recorded units are gone, and linking those requests by the
      inmate's current unit would be wrong, so the upgrade fails and the
      database must be restored from a backup.
    """
    existing = (
        {}
        if context.is_offline_mode()
        else {
            found['name']: found
            for found in sa.inspect(op.get_bind()).get_columns('requests')
        }
    )
    if 'unit_recorded' in existing and 'unit_name' not in existing:
        recorded = op.get_bind().execute(
            sa.text('SELECT COUNT(*) FROM requests WHERE unit_recorded')
        ).scalar_one()
        if recorded:
            raise RuntimeError(
                f"requests.unit_name is missing but {recorded} requests recorded "
                "a unit at fill: those units are lost; restore the database "
                "from the backup taken before the migration"
            )
    restore_recorded = 'unit_name' in existing and 'unit_recorded' not in existing
    for column in new_columns():
        if column.name in existing:
            check_existing(existing[column.name], column)
        else:
            op.add_column('requests', column)
    if restore_recorded:
        op.execute(
            sa.text('UPDATE requests SET unit_recorded = :yes WHERE unit_name IS NOT NULL')
            .bindparams(yes=True)
        )
        logger.warning(
            "requests.unit_recorded was missing: restored as recorded for every "
            "request with a unit name; requests filled while their inmate's "
            "unit was unknown could not be recovered and now count as older"
        )


def downgrade() -> None:
    """Downgrade schema.

    On SQLite the columns are dropped with a native ``ALTER TABLE ... DROP
    COLUMN``, not batch mode: rebuilding ``requests`` would drop the table,
    and with foreign keys on, that deletes every ``package_shipments`` row
    (``ON DELETE CASCADE``). Each is dropped only if it is there, so a
    downgrade interrupted after dropping one can be run again.
    """
    existing = existing_columns()
    for column in reversed(new_columns()):
        if existing and column.name not in existing:
            continue
        if op.get_context().dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE requests DROP COLUMN {column.name}')
        else:
            op.drop_column('requests', column.name)
