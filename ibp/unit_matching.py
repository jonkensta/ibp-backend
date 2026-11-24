"""Finding the IBP unit a postage label went to, from its tag or address.

Only the destination's evidence is used (``UnitMatcher``); the label's
recipient is never stored.
"""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any, NamedTuple, Optional

from . import models
from .unit_places import Places, UnitKey, display_name, line_numbers, squeeze, zip5


class Destination(NamedTuple):
    """Where a label's address says it went: a unit (its place's
    representative), or none; ``conflict`` when the address's name and its
    street or P.O. box point to different places, so nothing about where it
    went can be trusted."""

    unit: Optional[UnitKey]
    conflict: bool = False


class UnitMatcher:
    """Finds the IBP unit a label went to, by its tag, name or address."""

    def __init__(self, units: Iterable[models.Unit]):
        units = list(units)
        self._places = Places(units)
        self._by_tag: dict[UnitKey, UnitKey] = {}
        self._by_name: dict[str, list[tuple[str, str, Optional[str]]]] = defaultdict(
            list
        )
        self._by_number: dict[tuple[tuple[str, str], str], set[UnitKey]] = defaultdict(
            set
        )
        for unit in units:
            key = (unit.jurisdiction, unit.name)
            self._by_tag[(unit.jurisdiction, unit.name.upper())] = key
            zipcode = zip5(unit.zipcode)
            for name in {squeeze(unit.name), squeeze(display_name(unit))} - {""}:
                self._by_name[name].append((*key, zipcode))
            if zipcode is not None:
                for number in line_numbers((unit.street1, unit.street2)):
                    self._by_number[(number, zipcode)].add(key)

    def by_tag(self, jurisdiction: str, name: str) -> Optional[UnitKey]:
        """The unit a tag names, if IBP has it (unit names match in any case)."""
        return self._by_tag.get((jurisdiction, name.upper()))

    def by_address(self, address: dict[str, Any]) -> Optional[UnitKey]:
        """The one place a label's address names and is at, or None (see
        ``locate``)."""
        return self.locate(address).unit

    def locate(self, address: dict[str, Any]) -> Destination:
        """Where a label's address says it went.

        Units store the street in either line (usually the unit's display
        name in ``street1`` and the street in ``street2``), and EasyPost's
        verified addresses come back in USPS spelling with the name moved to
        ``street2``, so whole lines are not compared. Two kinds of evidence
        are gathered apart:

        - by name: a ``street1``, ``street2`` or ``company`` line that is a
          unit's key name or display name (``unit_places.display_name``;
          letters and digits only, any case) names that unit, whatever the
          ZIP. A name units in more than one place have (Texas and Federal,
          or one display name in two towns) counts only if the label's ZIP
          leaves one place;
        - by address: the house number or P.O. box number either street
          line starts with, with the label's ZIP, is one of these units'.

        Only a valid ZIP (``unit_places.zip5``) on both sides counts.
        Without a name, the address must find units of one place. With a
        name and no address evidence, the name's place stands. With both,
        the name's place must be among the address's: if it isn't, or if no
        place the name could mean is, the evidence conflicts (no unit, and
        ``conflict``). A name that stays ambiguous gives no unit. Units in
        different places at one address are told apart only by name. The
        unit returned is its place's representative
        (``unit_places.Places``), so units that are one place give one
        answer.
        """
        zipcode = zip5(address.get("zip"))
        named = {
            unit
            for field in ("street1", "street2", "company")
            for unit in self._by_name.get(squeeze(address.get(field)), [])
        }
        at_address: set[UnitKey] = set()
        if zipcode is not None:
            for number in line_numbers(
                (address.get("street1"), address.get("street2"))
            ):
                at_address |= self._by_number.get((number, zipcode), set())
        if not named:
            return Destination(self._places.one_place(at_address))
        place = self._places.one_place((j, name) for j, name, _ in named)
        if place is None and zipcode is not None:
            place = self._places.one_place(
                (j, name) for j, name, unit_zip in named if unit_zip == zipcode
            )
        if not at_address:
            return Destination(place)
        address_places = {self._places.representative(unit) for unit in at_address}
        if place in address_places:
            return Destination(place)
        name_places = {self._places.representative((j, name)) for j, name, _ in named}
        if place is not None or not name_places & address_places:
            return Destination(None, conflict=True)
        return Destination(None)
