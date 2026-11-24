"""Units' names as labels print them, and units that are one place.

A unit's key ``name`` is the provider's short code (``PLANE JAIL``), while a
label carries the name the unit goes by (``Plane Unit``), which the units
table keeps as an address line: its display name (``display_name``).

Some rows are one facility under several provider codes (a unit and its
trusty camp, say), with one display name and address. ``Places`` groups
them: units are the same place when they have the same jurisdiction, the
same display name (letters and digits only, any case) and the same 5-digit
ZIP. The street text isn't compared, since rows spell one street
differently (``1604 S. First St.``, ``1604 South First Street``), and a
shared street address alone doesn't make one place: units with different
display names at one address are different units. A place is named by its
alphabetically first unit, its representative.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from . import models

UnitKey = tuple[str, str]

# A squeezed line that is a P.O. box (``P.O. Box 9000``, ``Post Office
# Drawer 4``, ``Box 9``).
_PO_BOX = re.compile(r"^(P(OST)?O(FFICE)?(BOX|DRAWER)|(BOX|DRAWER)\d)")


def squeeze(text: Optional[str]) -> str:
    """Uppercase letters and digits only (``O'Daniel Unit`` is ``ODANIELUNIT``)."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def zip5(zipcode: Optional[str]) -> str:
    """The first five digits of a ZIP code."""
    return re.sub(r"\D", "", zipcode or "")[:5]


def display_name(unit: models.Unit) -> Optional[str]:
    """The unit's name as a label prints it, or None: the first of its
    ``street1`` and ``street2`` that doesn't start with a digit and isn't a
    P.O. box."""
    for line in (unit.street1, unit.street2):
        text = (line or "").strip()
        squeezed = squeeze(text)
        if squeezed and not text[0].isdigit() and not _PO_BOX.match(squeezed):
            return text
    return None


def _place_key(unit: models.Unit) -> tuple:
    name = squeeze(display_name(unit))
    zipcode = zip5(unit.zipcode)
    if name and len(zipcode) == 5:
        return ("place", unit.jurisdiction, name, zipcode)
    # No display name or no full ZIP: a place of its own.
    return ("unit", unit.jurisdiction, unit.name)


class Places:
    """Which units are the same place (see the module docstring)."""

    def __init__(self, units: Iterable[models.Unit]):
        groups: dict[tuple, list[UnitKey]] = defaultdict(list)
        for unit in units:
            groups[_place_key(unit)].append((unit.jurisdiction, unit.name))
        self._representative: dict[UnitKey, UnitKey] = {}
        self._members: dict[UnitKey, frozenset[UnitKey]] = {}
        for keys in groups.values():
            representative = min(keys)
            self._members[representative] = frozenset(keys)
            for key in keys:
                self._representative[key] = representative

    def representative(self, unit: UnitKey) -> Optional[UnitKey]:
        """The representative of the unit's place, or None for a unit IBP
        doesn't have."""
        return self._representative.get(unit)

    def members(self, unit: UnitKey) -> frozenset[UnitKey]:
        """Every unit of the unit's place (none for a unit IBP doesn't have)."""
        representative = self._representative.get(unit)
        if representative is None:
            return frozenset()
        return self._members[representative]

    def one_place(self, units: Iterable[UnitKey]) -> Optional[UnitKey]:
        """The representative of the one place all ``units`` are in, or None
        if there are none, or they are in several places or unknown."""
        places = {self._representative.get(unit) for unit in units}
        if len(places) != 1:
            return None
        return places.pop()
