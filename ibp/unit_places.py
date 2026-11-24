"""Units' names as labels print them, and units that are one place.

A unit's key ``name`` is the provider's short code (``PLANE JAIL``), while a
label carries the name the unit goes by (``Plane Unit``), which the units
table keeps as an address line: its display name (``display_name``). The
rule for it is conservative: a line that might be a street (``FM 686``,
``Prison Road``) is not a name, and a unit without a line that is plainly
one has no display name, and is just not matched by one.

Some rows are one facility under several provider codes (a unit and its
trusty camp, say). ``Places`` groups them: units are the same place when
they have the same jurisdiction, the same display name (letters and digits
only, any case), the same valid 5-digit ZIP, and share a house number or a
P.O. box number. The rest of the street isn't compared, since rows spell
one street differently (``1604 S. First St.``, ``1604 South First
Street``); units with no number at all are never merged, nor are units with
different display names at one address. A place is named by its
alphabetically first unit, its representative.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from . import models

UnitKey = tuple[str, str]

# A squeezed line that is a P.O. box (``P.O. Box 9000``, ``Post Office
# Drawer 4``, ``Box 9``), with its number when it has one.
_PO_BOX = re.compile(
    r"^(?:P(?:OST)?O(?:FFICE)?(?:BOX|DRAWER)|(?:BOX|DRAWER)(?=\d))(\d*)"
)
_LEADING_NUMBER = re.compile(r"\s*(\d+)(?!\d)")

# Words a facility's name has; a line without one is not taken as a name.
FACILITY_WORDS = frozenset(
    {
        "UNIT",
        "JAIL",
        "FACILITY",
        "COMPLEX",
        "CAMP",
        "CENTER",
        "CENTRE",
        "HOSPITAL",
        "INSTITUTION",
        "PENITENTIARY",
        "PRISON",
        "ANNEX",
        "CORRECTIONAL",
        "DETENTION",
        "CI",
        "FCI",
        "USP",
        "FPC",
        "FMC",
        "FDC",
        "MCC",
        "MDC",
        "FCC",
        "FTC",
        "SFF",
    }
)
# Words a street line starts with (``FM 686``, ``County Road 19``).
ROAD_LEADERS = frozenset(
    {
        "FM",
        "RM",
        "RR",
        "HWY",
        "HIGHWAY",
        "ROAD",
        "RD",
        "CR",
        "COUNTY",
        "SPUR",
        "IH",
        "I",
        "INTERSTATE",
        "US",
        "SH",
        "STATE",
        "LOOP",
        "ROUTE",
        "RT",
        "RTE",
        "FARM",
        "RANCH",
        "PO",
        "P",
        "BOX",
        "DRAWER",
    }
)
# Words a street line ends with (``Prison Road``, ``Camp Street``).
ROAD_ENDINGS = frozenset(
    {
        "ROAD",
        "RD",
        "STREET",
        "ST",
        "AVENUE",
        "AVE",
        "AV",
        "DRIVE",
        "DR",
        "LANE",
        "LN",
        "BOULEVARD",
        "BLVD",
        "PARKWAY",
        "PKWY",
        "WAY",
        "TRAIL",
        "TRL",
        "CIRCLE",
        "CIR",
        "COURT",
        "CT",
        "PLACE",
        "PL",
        "HIGHWAY",
        "HWY",
        "LOOP",
        "PIKE",
        "FREEWAY",
        "FWY",
        "EXPRESSWAY",
        "EXPY",
        "SPUR",
    }
)
DIRECTIONS = frozenset({"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"})
ROAD_WORDS = ROAD_LEADERS | ROAD_ENDINGS


def squeeze(text: Optional[str]) -> str:
    """Uppercase letters and digits only (``O'Daniel Unit`` is ``ODANIELUNIT``)."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def words(text: Optional[str]) -> list[str]:
    """The uppercase words (runs of letters and digits) of ``text``."""
    return re.findall(r"[A-Z0-9]+", (text or "").upper())


_ZIP = re.compile(r"\s*(\d{5})(?:-?\d{4})?\s*")


def zip5(zipcode: Optional[str]) -> Optional[str]:
    """The 5-digit ZIP of a well-formed ZIP or ZIP+4 (``77000``,
    ``77000-1234``), or None: ``77-000`` or an empty ZIP is no ZIP, so two
    of them never compare equal."""
    match = _ZIP.fullmatch(zipcode or "")
    return match[1] if match else None


def leading_number(line: Optional[str]) -> Optional[str]:
    """The number a line starts with, if any (``264`` of ``264 FM 3478``)."""
    match = _LEADING_NUMBER.match(line or "")
    return match[1] if match else None


def po_box(line: Optional[str]) -> Optional[str]:
    """A P.O. box line's number (``9000`` of ``P.O. Box 9000``), ``""`` for
    a box line without one, or None if the line is not a P.O. box."""
    match = _PO_BOX.match(squeeze(line))
    return match[1] if match else None


def is_street(tokens: list[str]) -> bool:
    """Whether a line's words read as a street rather than a name."""
    first = 1 if len(tokens) > 1 and tokens[0] in DIRECTIONS else 0
    return (
        tokens[first] in ROAD_LEADERS
        or tokens[-1] in ROAD_ENDINGS
        or all(token in ROAD_WORDS or token.isdigit() for token in tokens)
        or any(
            token in ROAD_WORDS and following.isdigit()
            for token, following in zip(tokens, tokens[1:])
        )
    )


def is_name(line: Optional[str]) -> bool:
    """Whether an address line is plainly a facility's name: not starting
    with a digit, not a P.O. box, not reading as a street, and holding a
    facility word (``Unit``, ``Jail``, ``FCI``...)."""
    text = (line or "").strip()
    tokens = words(text)
    return bool(
        tokens
        and not text[0].isdigit()
        and po_box(text) is None
        and not is_street(tokens)
        and FACILITY_WORDS & set(tokens)
    )


def display_name(unit: models.Unit) -> Optional[str]:
    """The unit's name as a label prints it: the first of its ``street1``
    and ``street2`` that ``is_name``, or None."""
    for line in (unit.street1, unit.street2):
        if is_name(line):
            return (line or "").strip()
    return None


def address_numbers(unit: models.Unit) -> set[tuple[str, str]]:
    """The unit's house numbers and P.O. box numbers, tagged by kind."""
    return line_numbers((unit.street1, unit.street2))


def line_numbers(lines: Iterable[Optional[str]]) -> set[tuple[str, str]]:
    """The house numbers and P.O. box numbers address lines start with,
    tagged by kind (``("house", "264")``, ``("box", "9000")``)."""
    numbers = set()
    for line in lines:
        house = leading_number(line)
        if house is not None:
            numbers.add(("house", house))
        box = po_box(line)
        if box:
            numbers.add(("box", box))
    return numbers


def _same_place_groups(units: list[models.Unit]) -> list[list[UnitKey]]:
    """Units of one name and ZIP, joined when they share a number."""
    parent = list(range(len(units)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return parent[i]

    by_number: dict[tuple[str, str], int] = {}
    for i, unit in enumerate(units):
        for number in address_numbers(unit):
            if number in by_number:
                parent[root(i)] = root(by_number[number])
            else:
                by_number[number] = i
    groups: dict[int, list[UnitKey]] = defaultdict(list)
    for i, unit in enumerate(units):
        groups[root(i)].append((unit.jurisdiction, unit.name))
    return list(groups.values())


class Places:
    """Which units are the same place (see the module docstring)."""

    def __init__(self, units: Iterable[models.Unit]):
        candidates: dict[tuple[str, str, str], list[models.Unit]] = defaultdict(list)
        groups: list[list[UnitKey]] = []
        for unit in units:
            name = squeeze(display_name(unit))
            zipcode = zip5(unit.zipcode)
            if name and zipcode is not None:
                candidates[(unit.jurisdiction, name, zipcode)].append(unit)
            else:
                groups.append([(unit.jurisdiction, unit.name)])
        for same_name in candidates.values():
            groups.extend(_same_place_groups(same_name))
        self._representative: dict[UnitKey, UnitKey] = {}
        self._members: dict[UnitKey, frozenset[UnitKey]] = {}
        for keys in groups:
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
