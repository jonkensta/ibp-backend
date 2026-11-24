"""Finding the IBP unit a postage label went to, from its tag or address.

Only the destination's evidence is used (``UnitMatcher``); the label's
recipient is never stored.
"""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any, NamedTuple, Optional

from . import models
from .unit_places import (
    Places,
    UnitKey,
    display_name,
    po_box,
    same_street,
    squeeze,
    street_tokens,
    zip5,
)


class Destination(NamedTuple):
    """Where a label's address says it went: a unit (its place's
    representative), or none; ``conflict`` when the address's name and its
    street or P.O. box point to different places, so nothing about where it
    went can be trusted."""

    unit: Optional[UnitKey]
    conflict: bool = False


_RANK = {True: 2, None: 1, False: 0}


class AddressEvidence(NamedTuple):
    """What a label's street lines and P.O. box say, with its ZIP: units
    whose delivery line they confirm, units at the same house number they
    can't be told from, and every unit at those numbers."""

    confirmed: set[UnitKey]
    undecided: set[UnitKey]
    at_number: set[UnitKey]


class UnitMatcher:
    """Finds the IBP unit a label went to, by its tag, name or address."""

    def __init__(self, units: Iterable[models.Unit]):
        units = list(units)
        self._places = Places(units)
        self._by_tag: dict[UnitKey, UnitKey] = {}
        self._by_name: dict[str, list[tuple[str, str, Optional[str]]]] = defaultdict(
            list
        )
        # (house number, ZIP) -> (unit, its street line); (box, ZIP) -> units.
        self._by_street: dict[tuple[str, str], list[tuple[UnitKey, str]]] = defaultdict(
            list
        )
        self._by_box: dict[tuple[str, str], set[UnitKey]] = defaultdict(set)
        for unit in units:
            key = (unit.jurisdiction, unit.name)
            self._by_tag[(unit.jurisdiction, unit.name.upper())] = key
            zipcode = zip5(unit.zipcode)
            for name in {squeeze(unit.name), squeeze(display_name(unit))} - {""}:
                self._by_name[name].append((*key, zipcode))
            if zipcode is None:
                continue
            for line in (unit.street1, unit.street2):
                tokens = street_tokens(line)
                if tokens:
                    self._by_street[(tokens[0], zipcode)].append((key, line or ""))
                box = po_box(line)
                if box:
                    self._by_box[(box, zipcode)].add(key)

    def by_tag(self, jurisdiction: str, name: str) -> Optional[UnitKey]:
        """The unit a tag names, if IBP has it (unit names match in any case)."""
        return self._by_tag.get((jurisdiction, name.upper()))

    def by_address(self, address: dict[str, Any]) -> Optional[UnitKey]:
        """The one place a label's address names and is at, or None (see
        ``locate``)."""
        return self.locate(address).unit

    def address_evidence(self, address: dict[str, Any]) -> AddressEvidence:
        """What the label's ``street1`` and ``street2`` say (see ``locate``)."""
        evidence = AddressEvidence(set(), set(), set())
        zipcode = zip5(address.get("zip"))
        if zipcode is None:
            return evidence
        verdicts: dict[UnitKey, Optional[bool]] = {}
        for line in (address.get("street1"), address.get("street2")):
            box = po_box(line)
            if box:
                evidence.confirmed.update(self._by_box.get((box, zipcode), set()))
            tokens = street_tokens(line)
            if not tokens:
                continue
            for unit, unit_line in self._by_street.get((tokens[0], zipcode), []):
                verdict = same_street(line, unit_line)
                # Confirmed by any line beats can't-tell beats different.
                if unit not in verdicts or _RANK[verdict] > _RANK[verdicts[unit]]:
                    verdicts[unit] = verdict
        evidence.at_number.update(verdicts)
        evidence.confirmed.update(unit for unit, ok in verdicts.items() if ok)
        evidence.undecided.update(unit for unit, ok in verdicts.items() if ok is None)
        evidence.undecided.difference_update(evidence.confirmed)
        return evidence

    def locate(self, address: dict[str, Any]) -> Destination:
        """Where a label's address says it went.

        Units store the street in either line (usually the unit's display
        name in ``street1`` and the street in ``street2``), and EasyPost's
        verified addresses come back in USPS spelling with the name moved to
        ``street2``. Two kinds of evidence are gathered apart:

        - by name: a ``street1``, ``street2`` or ``company`` line that is a
          unit's key name or display name (``unit_places.display_name``;
          letters and digits only, any case) names that unit, whatever the
          ZIP. A name units in more than one place have (Texas and Federal,
          or one display name in two towns) counts only if the label's ZIP
          leaves one place;
        - by address (``address_evidence``), with the label's ZIP: a P.O.
          box number confirms the units with that box; a street line is
          compared with the street line of each unit at that house number
          (``unit_places.same_street``, which allows USPS's respelling,
          added suffixes and directionals): the same street confirms the
          unit; a street shown different, or one normalisation can't
          decide on, confirms nothing.

        Only a valid ZIP (``unit_places.zip5``) on both sides counts.
        Spelling drifts between our street and USPS's (a renamed street, a
        suffix spelled differently), so failing to prove two streets the
        same is no evidence of another destination: a conflict needs
        positive evidence for another place. With the name's place P:

        - the label's street (or P.O. box) is the same as P's: P, whatever
          other places share that address (the name tells them apart);
        - it is the same as another place's and not P's: conflict;
        - its house number and ZIP match no line of P's but some other
          place's: conflict;
        - otherwise P stands: no street line, a number no unit has, or P's
          number with a street that differs from P's or can't be told from
          it but is the same as no other place's (also when several places
          share the number and the street matches none of them).

        Without a name the address must say where the label went: the
        units whose street (or box) is the same must be of one place; with
        none, units whose street can't be told count only if every unit at
        that number and ZIP is of one place. A name that stays ambiguous
        gives no unit, and a conflict if the address supports none of the
        places it could mean.

        A conflict gives no unit and ``conflict``, so nothing about where
        the label went is trusted. Units in different places at one address
        are told apart only by name. The unit returned is its place's
        representative (``unit_places.Places``), so units that are one place
        give one answer.
        """
        zipcode = zip5(address.get("zip"))
        named = {
            unit
            for field in ("street1", "street2", "company")
            for unit in self._by_name.get(squeeze(address.get(field)), [])
        }
        evidence = self.address_evidence(address)
        places = self._places
        unique = len({places.representative(u) for u in evidence.at_number}) == 1
        if not named:
            if evidence.confirmed:
                return Destination(places.one_place(evidence.confirmed))
            return Destination(places.one_place(evidence.undecided) if unique else None)
        place = places.one_place((j, name) for j, name, _ in named)
        if place is None and zipcode is not None:
            place = places.one_place(
                (j, name) for j, name, unit_zip in named if unit_zip == zipcode
            )
        if place is None:
            return self._ambiguous_name(named, evidence)
        return self._named_place(place, evidence)

    def _named_place(self, place: UnitKey, evidence: AddressEvidence) -> Destination:
        """The destination when the name gives ``place`` (see ``locate``)."""

        def places_of(units: set[UnitKey]) -> set[Optional[UnitKey]]:
            return {self._places.representative(unit) for unit in units}

        confirmed = places_of(evidence.confirmed)
        if place in confirmed:
            return Destination(place)
        if confirmed:
            return Destination(None, conflict=True)  # another place's street
        at_number = places_of(evidence.at_number)
        if at_number and place not in at_number:
            return Destination(None, conflict=True)  # another place's number
        return Destination(place)

    def _ambiguous_name(
        self, named: set[tuple[str, str, Optional[str]]], evidence: AddressEvidence
    ) -> Destination:
        """The destination when the name stays ambiguous (see ``locate``)."""
        supporting = evidence.confirmed | evidence.undecided
        if not supporting:
            return Destination(None)
        name_places = {self._places.representative((j, n)) for j, n, _ in named}
        support_places = {self._places.representative(unit) for unit in supporting}
        return Destination(None, conflict=not name_places & support_places)
