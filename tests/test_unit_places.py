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


@pytest.mark.parametrize(
    "first, second",
    [
        ("1604 S. First St.", "1604 South First Street"),
        ("1400 Industrial Blvd ", "1400 Industrial Blvd"),
        ("2665 Prison Road #1", "2665 Prison Road #1"),
        ("2665 Prison Road #1", "2665 Prison Rd."),
        ("11034 Hwy 36", "11034 Hwy 36"),
        ("11034 Highway 36", "11034 HWY 36"),
        ("P.O. Box 9000", "PO BOX 9000"),
    ],
)
def test_same_delivery_line_spelled_two_ways_is_one_place(first, second):
    """Two rows with one name and ZIP whose delivery lines normalise alike
    are one place."""
    places = Places(
        [
            unit("ONE", "River Valley Unit", first),
            unit("TWO", "River Valley Unit", second),
        ]
    )
    assert places.members(("Texas", "ONE")) == {("Texas", "ONE"), ("Texas", "TWO")}


@pytest.mark.parametrize(
    "first, second",
    [
        ("100 Main Street", "100 Oak Lane"),
        ("100 N Main St", "100 S Main St"),
        ("100 Main St", "1000 Main St"),
        ("P.O. Box 9000", "100 Main St"),
    ],
)
def test_same_number_on_another_street_is_another_place(first, second):
    """A shared house number on a different street doesn't make one place."""
    places = Places(
        [
            unit("ONE", "River Valley Unit", first),
            unit("TWO", "River Valley Unit", second),
        ]
    )
    assert places.one_place([("Texas", "ONE"), ("Texas", "TWO")]) is None


@pytest.mark.parametrize(
    "line, normalised",
    [
        ("100 Unit Road", ("street", "100 UNIT RD")),
        ("100 Building Lane", ("street", "100 BUILDING LN")),
        ("100 Suite Street", ("street", "100 SUITE ST")),
        ("100 Main St Apt 2B", ("street", "100 MAIN ST")),
        ("100 Main St Ste 100", ("street", "100 MAIN ST")),
        ("100 Main St Bldg C", ("street", "100 MAIN ST")),
        ("100 Main St Unit 4", ("street", "100 MAIN ST")),
        ("2665 Prison Road #1", ("street", "2665 PRISON RD")),
        ("100 Unit 4", ("street", "100 UNIT 4")),
        ("100", None),
        ("100 #4", None),
        ("100 2nd St", ("street", "100 2ND ST")),
        ("  100  ", None),
    ],
)
def test_delivery_line_keeps_the_street(line, normalised):
    """A marker word is stripped only after a whole street and before a
    designator; a bare house number is no delivery line."""
    from ibp.unit_places import delivery_line  # pylint: disable=import-outside-toplevel

    assert delivery_line(line) == normalised


@pytest.mark.parametrize(
    "first, second",
    [("100 Unit Road", "100 Building Lane"), ("100", "100"), ("100 Apt", "100 Ste")],
)
def test_streets_named_like_markers_are_different_places(first, second):
    """Streets named Unit Road and Building Lane, or lines that are only a
    number, never make one place."""
    places = Places(
        [
            unit("ONE", "River Valley Unit", first),
            unit("TWO", "River Valley Unit", second),
        ]
    )
    assert places.one_place([("Texas", "ONE"), ("Texas", "TWO")]) is None


# The real pairs of our street line and USPS's spelling of it (made-up units
# use the same shapes).
RESPELLED = [
    ("264 FM 3478", "264 FM 3478 RD"),
    ("1098 S. Highway 2037", "1098 S HIGHWAY 2037"),
    ("1401 State School Road", "1401 STATE SCHOOL RD"),
    ("3872 FM 350 South", "3872 FM 350 S"),
    ("810 FM 2821", "810 FM 2821 RD W"),
    ("1916 North Hwy. 36 Bypass", "1916 N HIGHWAY 36 BYP"),
]


@pytest.mark.parametrize(
    "ours, theirs, same",
    [
        *((ours, theirs, True) for ours, theirs in RESPELLED),
        ("100 Main Street", "100 Oak Lane", False),
        ("100 Main St", "100 Main Ave", False),
        ("100 N Main St", "100 S Main St", False),
        ("100 Main St", "101 Main St", False),
        ("100 Main St N", "100 MAIN ST S", False),
        ("100 FM 1", "100 FM 1 SPUR", None),
        ("100 Main St", "PO Box 100", None),
        ("100 Main St", None, None),
    ],
)
def test_same_street(ours, theirs, same):
    """USPS's respelling is the same street; another street, number,
    directional or suffix is not; more street words can't be told."""
    from ibp.unit_places import same_street  # pylint: disable=import-outside-toplevel

    assert same_street(ours, theirs) is same
    assert same_street(theirs, ours) is same
