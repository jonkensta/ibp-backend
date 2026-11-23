"""Rewrite Texas inmates' race and sex codes as words

Revision ID: d2a6f8c3e1b7
Revises: c4e7a2f9d1b3
Create Date: 2026-11-23 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd2a6f8c3e1b7'
down_revision: Union[str, None] = 'c4e7a2f9d1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A frozen copy of the TDCJ tables in ibp/providers/normalize.py as of this
# revision; a migration must not import application code. TDCJ publishes no
# legend: I, O and U are assumed meanings. Federal rows already hold words.
RACES = {
    'W': 'White',
    'B': 'Black',
    'H': 'Hispanic',
    'A': 'Asian',
    'I': 'American Indian',
    'O': 'Other',
    'U': 'Unknown',
}
SEXES = {
    'M': 'Male',
    'F': 'Female',
    'U': 'Unknown',
}


def _rewrite(column: str, mapping: dict[str, str]) -> None:
    """Rewrite Texas rows' ``column`` from each key of ``mapping`` to its value.

    Only values in the mapping are touched; NULL and anything else is left.
    """
    params = {}
    whens = []
    for number, (old, new) in enumerate(mapping.items()):
        params[f'old{number}'] = old
        params[f'new{number}'] = new
        whens.append(f'WHEN :old{number} THEN :new{number}')
    olds = ', '.join(f':old{number}' for number in range(len(mapping)))
    op.get_bind().execute(
        sa.text(
            f'UPDATE inmates SET {column} = CASE {column} {" ".join(whens)} END '
            f"WHERE jurisdiction = 'Texas' AND {column} IN ({olds})"
        ),
        params,
    )


def _reversed(mapping: dict[str, str]) -> dict[str, str]:
    return {new: old for old, new in mapping.items()}


def upgrade() -> None:
    """Upgrade data: Texas codes become the canonical words."""
    _rewrite('race', RACES)
    _rewrite('sex', SEXES)


def downgrade() -> None:
    """Downgrade data: Texas words go back to TDCJ's codes."""
    _rewrite('race', _reversed(RACES))
    _rewrite('sex', _reversed(SEXES))
