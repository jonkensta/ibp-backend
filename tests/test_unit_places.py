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
    ],
)
def test_display_name(street1, street2, name):
    """The first address line that isn't a street number or a P.O. box."""
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
