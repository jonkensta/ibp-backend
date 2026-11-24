"""Finding the IBP unit a postage label went to, from its tag or address.

Only the destination's evidence is used (``UnitMatcher``); the label's
recipient is never stored.
"""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Optional

from . import models
from .unit_places import Places, display_name, leading_number, squeeze, zip5


class UnitMatcher:
    """Finds the IBP unit a label went to, by its tag, name or address."""

    def __init__(self, units: Iterable[models.Unit]):
        units = list(units)
        self._places = Places(units)
        self._by_tag: dict[tuple[str, str], tuple[str, str]] = {}
        self._by_name: dict[str, list[tuple[str, str, Optional[str]]]] = defaultdict(
            list
        )
        self._by_number: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for unit in units:
            key = (unit.jurisdiction, unit.name)
            self._by_tag[(unit.jurisdiction, unit.name.upper())] = key
            for name in {squeeze(unit.name), squeeze(display_name(unit))} - {""}:
                self._by_name[name].append((*key, zip5(unit.zipcode)))
            zipcode = zip5(unit.zipcode)
            for street in (unit.street1, unit.street2):
                number = leading_number(street)
                if number is not None and zipcode is not None:
                    self._by_number[(number, zipcode)].add(key)

    def by_tag(self, jurisdiction: str, name: str) -> Optional[tuple[str, str]]:
        """The unit a tag names, if IBP has it (unit names match in any case)."""
        return self._by_tag.get((jurisdiction, name.upper()))

    def by_address(self, address: dict[str, Any]) -> Optional[tuple[str, str]]:
        """The one place a label's address names and is at, or None.

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
        - by address: the house number either street line starts with,
          with the label's ZIP, leads the ``street1`` or ``street2`` of
          these units.

        Only a valid ZIP (``unit_places.zip5``) on both sides counts. With a
        name, the result is its place, unless it is ambiguous, or the
        address finds units and the name's place is not among them (the
        street says one unit, the name another): then nothing. Without a
        name, the address must find units of one place. Units in different
        places at one address are told apart only by name. The unit
        returned is its place's representative (``unit_places.Places``),
        so units that are one place give one answer.
        """
        zipcode = zip5(address.get("zip"))
        named = {
            unit
            for field in ("street1", "street2", "company")
            for unit in self._by_name.get(squeeze(address.get(field)), [])
        }
        at_number: set[tuple[str, str]] = set()
        if zipcode is not None:
            for field in ("street1", "street2"):
                number = leading_number(address.get(field))
                if number is not None:
                    at_number |= self._by_number.get((number, zipcode), set())
        if not named:
            return self._places.one_place(at_number)
        place = self._places.one_place((j, name) for j, name, _ in named)
        if place is None and zipcode is not None:
            place = self._places.one_place(
                (j, name) for j, name, unit_zip in named if unit_zip == zipcode
            )
        if place is None or (
            at_number
            and place not in {self._places.representative(unit) for unit in at_number}
        ):
            return None
        return place
