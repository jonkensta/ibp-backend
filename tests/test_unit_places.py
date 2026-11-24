"""Units' display names and which units are one place (invented units)."""

import pytest

from ibp import models
from ibp.unit_places import Places, display_name


def unit(name, street1, street2=None, zipcode="77000", jurisdiction="Texas"):
    """An unsaved unit."""
    return models.Unit(
        jurisdiction=jurisdiction,
        name=name,
        street1=street1,
        street2=street2,
        city="Exampleton",
        zipcode=zipcode,
        state="TX",
    )


@pytest.mark.parametrize(
    "street1, street2, name",
    [
        ("Example Unit", "264 FM 3478", "Example Unit"),
        ("264 FM 3478", "Example Unit", "Example Unit"),
        ("P.O. Box 9000", "Example Unit", "Example Unit"),
        ("PO Box 9", None, None),
        ("Post Office Box 12", None, None),
        ("Box 77", None, None),
        ("P.O. Drawer 4", None, None),
        ("1401 State School Road", None, None),
        (" 12 Main St", "---", None),
        ("Boxwood Unit", None, "Boxwood Unit"),
        # Street lines without a house number are not names.
        ("FM 686", None, None),
        ("Highway 190", None, None),
        ("N Highway 190", None, None),
        ("County Road 19", None, None),
        ("Spur 591", None, None),
        ("Prison Road", None, None),
        ("IH 45", None, None),
        ("Camp Street", None, None),
        ("Example Unit Hwy 36", None, None),
        # A line with no facility word is not plainly a name.
        ("Darrington", None, None),
        ("Highway 190", "Example Unit", "Example Unit"),
        ("Example State Jail", None, "Example State Jail"),
        ("FCI Example", None, "FCI Example"),
    ],
)
def test_display_name(street1, street2, name):
    """The first address line that is plainly a facility's name: not a
    street, a street number or a P.O. box, and with a facility word."""
    assert display_name(unit("CODE", street1, street2)) == name


def test_places_group_one_name_and_zip():
    """Same jurisdiction, display name and 5-digit ZIP are one place, named
    by its alphabetically first unit; the street text may differ."""
    places = Places(
        [
            unit("MAIN", "Main Unit", "1604 S. First St.", "77000"),
            unit("CAMP", "MAIN UNIT", "1604 South First Street", "77000-1234"),
            unit("NEIGHBOR", "Neighbor Unit", "1604 S. First St.", "77000"),
            unit("ELSEWHERE", "Main Unit", "1 Other Rd", "77100"),
            unit("FEDERAL", "Main Unit", "1604 S. First St.", "77000", "Federal"),
            unit("NOZIP A", "Nozip Unit", zipcode=""),
            unit("NOZIP B", "Nozip Unit", zipcode=""),
            unit("STREET", "1 Street Rd"),
        ]
    )
    main = frozenset({("Texas", "MAIN"), ("Texas", "CAMP")})
    assert places.members(("Texas", "MAIN")) == main
    assert places.representative(("Texas", "MAIN")) == ("Texas", "CAMP")
    assert places.one_place(main) == ("Texas", "CAMP")
    for alone in ("NEIGHBOR", "ELSEWHERE", "NOZIP A", "NOZIP B", "STREET"):
        assert places.members(("Texas", alone)) == {("Texas", alone)}
    assert places.members(("Federal", "FEDERAL")) == {("Federal", "FEDERAL")}
    assert places.one_place([("Texas", "MAIN"), ("Texas", "NEIGHBOR")]) is None
    assert places.representative(("Texas", "GONE")) is None
    assert places.members(("Texas", "GONE")) == frozenset()
    assert places.one_place([("Texas", "GONE")]) is None
    assert places.one_place([]) is None


def test_same_name_and_zip_at_different_streets_are_not_one_place():
    """Two units with one display name and ZIP but different house numbers
    are different facilities."""
    places = Places(
        [
            unit("FIRST", "Twin Unit", "100 First Rd"),
            unit("SECOND", "Twin Unit", "200 Second Rd"),
        ]
    )
    assert places.members(("Texas", "FIRST")) == {("Texas", "FIRST")}
    assert places.members(("Texas", "SECOND")) == {("Texas", "SECOND")}


def test_units_without_numbers_are_never_one_place():
    """With no house or box number to share, a name and ZIP aren't enough."""
    places = Places([unit("FIRST", "Twin Unit"), unit("SECOND", "Twin Unit")])
    assert places.one_place([("Texas", "FIRST"), ("Texas", "SECOND")]) is None


def test_units_sharing_a_po_box_are_one_place():
    """A shared P.O. box number corroborates a name and ZIP."""
    places = Places(
        [
            unit("FIRST", "Bradley Unit", "P.O. Box 9000"),
            unit("SECOND", "BRADLEY UNIT", "PO Box 9000"),
            unit("THIRD", "Bradley Unit", "PO Box 9001"),
        ]
    )
    assert places.members(("Texas", "SECOND")) == {
        ("Texas", "FIRST"),
        ("Texas", "SECOND"),
    }
    assert places.members(("Texas", "THIRD")) == {("Texas", "THIRD")}
