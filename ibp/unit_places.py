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
only, any case), the same valid 5-digit ZIP, and share a delivery line: a
P.O. box number, or a house number and street that are the same once
spelling is normalised (``delivery_line``: ``1604 S. First St.`` and
``1604 South First Street`` are one street, ``100 Main Street`` and ``100
Oak Lane`` are not). Lines normalisation can't prove the same are different
places; units with no delivery line are never merged, nor are units with
different display names at one address. A place is named by its
alphabetically first unit, its representative.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import NamedTuple, Optional

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


# Spellings of one word in a street line, as their canonical form.
_STREET_WORDS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "STREET": "ST",
    "ROAD": "RD",
    "HIGHWAY": "HWY",
    "AVENUE": "AVE",
    "AV": "AVE",
    "BOULEVARD": "BLVD",
    "DRIVE": "DR",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "FREEWAY": "FWY",
    "COURT": "CT",
    "CIRCLE": "CIR",
    "TRAIL": "TRL",
    "PLACE": "PL",
    "BYPASS": "BYP",
    "EXPRESSWAY": "EXPY",
    "TERRACE": "TER",
    "FIRST": "1ST",
    "SECOND": "2ND",
    "THIRD": "3RD",
    "FOURTH": "4TH",
    "FIFTH": "5TH",
    "SIXTH": "6TH",
    "SEVENTH": "7TH",
    "EIGHTH": "8TH",
    "NINTH": "9TH",
    "TENTH": "10TH",
}
# A secondary unit marker ending a street line: ``#`` or a marker word
# followed by a designator (``#1``, ``Unit 4``, ``Apt 2B``, ``Ste 100``,
# ``Bldg C``). A designator has a digit first, or is one letter, so a street
# named with such a word (``Unit Road``, ``Building Lane``) is not one.
_DESIGNATOR = r"(?:[0-9][A-Z0-9-]*|[A-Z])"
_UNIT_MARKER = re.compile(
    rf"(?:\s*#\s*{_DESIGNATOR}|\s+(?:UNIT|APT|STE|SUITE|BLDG|BUILDING)\.?\s+"
    rf"{_DESIGNATOR})\s*$"
)


def street_tokens(line: Optional[str]) -> Optional[list[str]]:
    """A street line's words in one spelling, house number first, or None
    if it doesn't start with a house number followed by a street.

    The line is uppercased and its punctuation dropped, a trailing unit
    marker removed only when a street remains before it, and each word put
    in one spelling (``_STREET_WORDS``). A line with no word but numbers
    after its house number (``100``, ``100 #4``) is no street.
    """
    if leading_number(line) is None:
        return None
    text = (line or "").upper()
    stripped = _UNIT_MARKER.sub("", text)
    if len(words(stripped)) >= 2:
        text = stripped
    tokens = [_STREET_WORDS.get(token, token) for token in words(text)]
    if not tokens[0].isdigit() or all(token.isdigit() for token in tokens[1:]):
        return None  # no street word after the number
    return tokens


def delivery_line(line: Optional[str]) -> Optional[tuple[str, str]]:
    """A street line as compared between units: ``("box", number)`` for a
    numbered P.O. box, ``("street", normalised text)`` for a house number
    and street (``street_tokens``), else None (never equal to anything).
    """
    box = po_box(line)
    if box:
        return ("box", box)
    tokens = street_tokens(line)
    return ("street", " ".join(tokens)) if tokens else None


# A street's directionals and its type suffix, which USPS spells, adds or
# drops (``264 FM 3478`` comes back ``264 FM 3478 RD``).
_DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})
_SUFFIXES = frozenset(
    {
        "ST",
        "RD",
        "AVE",
        "BLVD",
        "DR",
        "LN",
        "PKWY",
        "FWY",
        "CT",
        "CIR",
        "TRL",
        "PL",
        "WAY",
        "BYP",
        "EXPY",
        "PIKE",
        "TER",
    }
)


class StreetParts(NamedTuple):
    """A street line split up: house number, leading directional, the
    street's own words, type suffix and trailing directional."""

    number: str
    lead: Optional[str]
    core: tuple[str, ...]
    suffix: Optional[str]
    trail: Optional[str]


def street_parts(tokens: list[str]) -> StreetParts:
    """Split ``street_tokens`` output; the street's own words are never
    taken away (a one-word street keeps its word)."""
    rest = list(tokens[1:])
    lead = rest.pop(0) if len(rest) > 1 and rest[0] in _DIRECTIONALS else None
    trail = rest.pop() if len(rest) > 1 and rest[-1] in _DIRECTIONALS else None
    suffix = rest.pop() if len(rest) > 1 and rest[-1] in _SUFFIXES else None
    return StreetParts(tokens[0], lead, tuple(rest), suffix, trail)


def _within(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """Whether ``short`` is a run of consecutive words of ``long``."""
    return any(
        long[i : i + len(short)] == short for i in range(len(long) - len(short) + 1)
    )


def same_street(first: Optional[str], second: Optional[str]) -> Optional[bool]:
    """Whether two street lines are the same house number on the same
    street: True if normalisation shows they are, False if it shows they
    are not, None if it can't tell.

    Lines are compared by ``street_parts``. Different numbers, or street
    words neither contained in the other, are different. The same street
    words are the same street unless both lines give a directional or
    suffix and they differ (``100 Main St`` / ``100 Main Ave``); one line
    adding or dropping one is the same (``264 FM 3478`` / ``264 FM 3478
    RD``, ``810 FM 2821`` / ``810 FM 2821 RD W``). Street words one line
    has more of (``FM 1`` / ``FM 1 SPUR``) can't be told: None. So can a
    line that isn't a street.
    """
    first_tokens, second_tokens = street_tokens(first), street_tokens(second)
    if not first_tokens or not second_tokens:
        return None
    one, other = street_parts(first_tokens), street_parts(second_tokens)
    if one.number != other.number:
        return False
    if one.core != other.core:
        if _within(one.core, other.core) or _within(other.core, one.core):
            return None
        return False
    for mine, theirs in (
        (one.lead, other.lead),
        (one.suffix, other.suffix),
        (one.trail, other.trail),
    ):
        if mine and theirs and mine != theirs:
            return False
    return True


def delivery_lines(unit: models.Unit) -> set[tuple[str, str]]:
    """The unit's delivery lines (``delivery_line``)."""
    found = {delivery_line(line) for line in (unit.street1, unit.street2)}
    return {line for line in found if line is not None}


def _same_place_groups(units: list[models.Unit]) -> list[list[UnitKey]]:
    """Units of one name and ZIP, joined when they share a delivery line."""
    parent = list(range(len(units)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return parent[i]

    by_line: dict[tuple[str, str], int] = {}
    for i, unit in enumerate(units):
        for line in delivery_lines(unit):
            if line in by_line:
                parent[root(i)] = root(by_line[line])
            else:
                by_line[line] = i
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
