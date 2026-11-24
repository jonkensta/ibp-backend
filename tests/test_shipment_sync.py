"""The EasyPost shipment sync, against a fake EasyPost with invented data."""

# pylint: disable=redefined-outer-name

import datetime
import email.message
import http.client
import io
import json
import logging
import sqlite3
import urllib.error
from pathlib import Path

import pytest
import sqlalchemy as sa

from ibp import (
    db_bootstrap,
    easypost_client,
    label_reading,
    models,
    shipment_links,
    shipment_sync,
)
from ibp.labels import RequestKey
from ibp.shipment_sync import (
    EasyPost,
    Tag,
    UnitMatcher,
    inmate_number,
    is_finished,
    month_windows,
    parse_reference,
)

from .easypost_fake import NAME, STREET, FakeEasyPost, event, shipment
from .test_unit_places import RESPELLED
from .shipment_helpers import add_inmate, add_request, add_shipment, add_unit, utc

NOW = utc("2026-03-10T12:00")
DAY = datetime.timedelta(days=1)
KEY = "EZAK" + "0" * 28


def ago(days: float) -> datetime.datetime:
    """``days`` before NOW."""
    return NOW - days * DAY


@pytest.fixture
def sessions(api_db):
    """The test database's sessionmaker, with unit ALPHA at the fake address
    (and BETA, another Box unit, elsewhere)."""
    with api_db() as session:
        add_unit(session, "ALPHA", street1="100 Example Rd", zipcode="77000")
        add_unit(session, "BETA", street1="200 Other Way", zipcode="77001")
        session.commit()
    return api_db


@pytest.fixture
def logs(caplog, monkeypatch):
    """caplog, with the sync's logger enabled (alembic's fileConfig in the
    migration tests disables existing loggers)."""
    monkeypatch.setattr(shipment_sync.logger, "disabled", False)
    return caplog


def sync(fake: FakeEasyPost, sessions, now=NOW, since=None, sleeps=None):
    """Run the sync against ``fake``."""
    client = EasyPost(
        KEY, urlopen=fake, sleep=(sleeps if sleeps is not None else []).append
    )
    return shipment_sync.run(client, sessions, now=now, since=since)


def snapshot(sessions) -> dict[str, list[tuple]]:
    """Every row of the three tables."""
    with sessions() as session:
        return {
            table: [
                tuple(row)
                for row in session.execute(
                    sa.text(f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4")  # nosec
                )
            ]
            for table in ("shipments", "tracking_events", "package_shipments")
        }


def stored(sessions, easypost_id: str) -> models.Shipment:
    """A stored label, with its events loaded."""
    with sessions() as session:
        row = session.scalars(
            sa.select(models.Shipment).where(models.Shipment.easypost_id == easypost_id)
        ).one()
        _ = row.events
        session.expunge(row)
        return row


# --- the reference tag ---------------------------------------------------------


@pytest.mark.parametrize(
    "reference, tag",
    [
        (
            "ibp1:req:TEX-00012345-2",
            Tag("request", request=RequestKey("Texas", 12345, 2)),
        ),
        (
            " ibp1:req:FED-87654321-0 ",
            Tag("request", request=RequestKey("Federal", 87654321, 0)),
        ),
        ("ibp1:unit:Texas:BETO", Tag("unit", unit=("Texas", "BETO"))),
        ("ibp1:unit:federal:FCI EXAMPLE", Tag("unit", unit=("Federal", "FCI EXAMPLE"))),
        ("ibp1:manual", Tag("manual")),
    ],
)
def test_parse_reference_tags(reference, tag):
    """The three tag forms are read."""
    assert parse_reference(reference) == tag


@pytest.mark.parametrize(
    "reference",
    [
        None,
        "",
        "   ",
        "order 1234",
        "ibp1:",
        "ibp1:req:",
        "ibp1:req:TEX-ABC-1",
        "ibp1:req:XYZ-00012345-2",
        "ibp1:unit:Texas",
        "ibp1:unit:Texas:",
        "ibp1:unit:Ohio:BETO",
        "ibp1:manual:extra",
        "ibp2:req:TEX-00012345-2",
        "IBP1:MANUAL",
    ],
)
def test_parse_reference_treats_anything_else_as_absent(reference):
    """Junk, empty and unknown text is no tag."""
    assert parse_reference(reference) is None


@pytest.mark.parametrize(
    "addressee, number",
    [
        ("Testy Example #00012345", "00012345"),
        ("Testy Q. Example-Person #12345678 ", "12345678"),
        ("Testy Example", None),
        ("Testy Example #123456", None),
        ("Testy Example #123456789", None),
        ("ATTN: MAILROOM STAFF", None),
        (None, None),
    ],
)
def test_inmate_number_from_addressee(addressee, number):
    """Only an 8-digit number ending the line is an inmate number."""
    assert inmate_number(addressee) == number


def test_unit_matching_by_tag(api_db):
    """Tags match unit names in any case, in the tag's jurisdiction only."""
    with api_db() as session:
        alpha = add_unit(session, "ALPHA", street1="1 Shared Rd", zipcode="77002")
        matcher = UnitMatcher([alpha])
    assert matcher.by_tag("Texas", "alpha") == ("Texas", "ALPHA")
    assert matcher.by_tag("Federal", "ALPHA") is None


@pytest.fixture
def matcher(api_db):
    """Units shaped like the real table (invented names and addresses)."""
    with api_db() as session:
        units = [
            # The name in street1 and the street in street2, as most rows are.
            add_unit(
                session,
                "Example Unit",
                street1="Example Unit",
                street2="264 FM 3478",
                zipcode="77320",
            ),
            add_unit(
                session,
                " Testy Q. O'Sample Unit",
                street1="Testy Q. O'Sample Unit",
                street2="1098 S. Highway 2037",
                zipcode="79501",
            ),
            # The street in street1.
            add_unit(
                session,
                "Schoolhouse",
                street1="1401 State School Road",
                zipcode="77080",
            ),
            # Two units at one address.
            add_unit(
                session,
                "Twin A Unit",
                street1="Twin A Unit",
                street2="2665 Prison Road #1",
                zipcode="77351",
            ),
            add_unit(
                session,
                "Twin B Unit",
                street1="Twin B Unit",
                street2="2665 Prison Road #1",
                zipcode="77351",
            ),
            # One name in both jurisdictions.
            add_unit(session, "Doubled", street1="5 Texas Way", zipcode="77500"),
            add_unit(
                session,
                "Doubled",
                jurisdiction="Federal",
                street1="5 Federal Way",
                zipcode="77600",
                shipping_method="Federal Tub",
            ),
        ]
        return UnitMatcher(units)


@pytest.mark.parametrize(
    "address, unit",
    [
        # Verified: the street in USPS spelling, the name in street2.
        (
            {
                "street1": "264 FM 3478 RD",
                "street2": "EXAMPLE UNIT",
                "zip": "77320-6522",
            },
            ("Texas", "Example Unit"),
        ),
        (
            {
                "street1": "1098 S HIGHWAY 2037",
                "street2": "TESTY Q OSAMPLE UNIT",
                "zip": "79501",
            },
            ("Texas", " Testy Q. O'Sample Unit"),
        ),
        # Verified, without the name: by house number and ZIP.
        (
            {"street1": "1401 STATE SCHOOL RD", "zip": "77080-1234"},
            ("Texas", "Schoolhouse"),
        ),
        ({"street1": "264 FM 3478 RD", "zip": "77320"}, ("Texas", "Example Unit")),
        # Unverified: the name in street1, the street in street2.
        (
            {"street1": "Example Unit", "street2": "264 FM 3478", "zip": "77320"},
            ("Texas", "Example Unit"),
        ),
        # The name in the company line.
        (
            {"company": "Schoolhouse", "street1": "PO Box 9", "zip": "77080"},
            ("Texas", "Schoolhouse"),
        ),
        # Units sharing an address are told apart by name only.
        (
            {"street1": "2665 PRISON RD # 1", "street2": "TWIN B UNIT", "zip": "77351"},
            ("Texas", "Twin B Unit"),
        ),
        ({"street1": "2665 PRISON RD # 1", "zip": "77351"}, None),
        # A name two jurisdictions have: the ZIP decides, or nothing does.
        ({"street1": "DOUBLED", "zip": "77600"}, ("Federal", "Doubled")),
        ({"street1": "DOUBLED", "zip": "77000"}, None),
        # A house number in another ZIP, or no number and no name.
        ({"street1": "264 FM 3478 RD", "zip": "77321"}, None),
        ({"street1": "2640 FM 3478 RD", "zip": "77320"}, None),
        ({"street1": "Somewhere Else", "zip": "77320"}, None),
        ({}, None),
    ],
)
def test_unit_matching_by_address(matcher, address, unit):
    """A label's lines name a unit, or its house number and ZIP find one."""
    assert matcher.by_address(address) == unit


@pytest.fixture
def coded_matcher(api_db):
    """Units keyed by provider codes that differ from the names labels print
    (invented codes, names and addresses)."""
    with api_db() as session:
        units = [
            # Two units at one address, told apart by display name only.
            add_unit(
                session,
                "PLAIN JAIL",
                street1="Plain Unit",
                street2="904 FM 686",
                zipcode="77535",
            ),
            add_unit(
                session,
                "SANTA BABY BOND",
                street1="Santa Baby Bonding Unit",
                street2="904 FM 686",
                zipcode="77535",
            ),
            # A P.O. box for the street.
            add_unit(
                session,
                "BRADLEY",
                street1="Bradley Unit",
                street2="P.O. Box 9000",
                zipcode="75600",
            ),
            add_unit(
                session, "MURRAYS", street1="Murrays Unit", street2="1916 N Hwy 36"
            ),
            # One place under two codes (the street spelled two ways), and a
            # different unit at the same street address.
            add_unit(
                session,
                "WAINRIGHT",
                street1="J. Doe Wainright Unit",
                street2="2665 Prison Road #1",
                zipcode="75851",
            ),
            add_unit(
                session,
                "J. DOE WAINRIGHT",
                street1="J. Doe Wainright Unit",
                street2="2665 Prison Rd. #1",
                zipcode="75851-1234",
            ),
            add_unit(
                session,
                "EASTMAN",
                street1="Eastman Unit",
                street2="2665 Prison Road #1",
                zipcode="75851",
            ),
            # One display name in two towns.
            add_unit(
                session,
                "TWINTOWN NORTH",
                street1="Twin Town Unit",
                street2="1 North Rd",
                zipcode="76000",
            ),
            add_unit(
                session,
                "TWINTOWN SOUTH",
                street1="Twin Town Unit",
                street2="1 South Rd",
                zipcode="76100",
            ),
        ]
        return UnitMatcher(units)


@pytest.mark.parametrize(
    "address, unit",
    [
        # The display name, where the key name differs.
        (
            {"street1": "904 FM 686", "street2": "PLAIN UNIT", "zip": "77535"},
            ("Texas", "PLAIN JAIL"),
        ),
        (
            {"street1": "SANTA BABY BONDING UNIT", "zip": "77535"},
            ("Texas", "SANTA BABY BOND"),
        ),
        # The shared address alone is ambiguous.
        ({"street1": "904 FM 686", "zip": "77535"}, None),
        # The key name still matches.
        ({"company": "Plain Jail", "street1": "904 FM 686"}, ("Texas", "PLAIN JAIL")),
        # A P.O. box unit, by name.
        (
            {"street1": "PO BOX 9000", "street2": "BRADLEY UNIT", "zip": "75600"},
            ("Texas", "BRADLEY"),
        ),
        # A unique name in another ZIP than the unit's.
        (
            {"street1": "1916 N HWY 36", "street2": "MURRAYS UNIT", "zip": "76528"},
            ("Texas", "MURRAYS"),
        ),
        # One place under two codes: its representative, by name or address.
        (
            {"street1": "2665 PRISON RD # 1", "street2": "J DOE WAINRIGHT UNIT"},
            ("Texas", "J. DOE WAINRIGHT"),
        ),
        ({"street1": "Wainright", "zip": "75851"}, ("Texas", "J. DOE WAINRIGHT")),
        # The different unit at that address is not merged with it.
        (
            {"street1": "2665 PRISON RD # 1", "street2": "EASTMAN UNIT"},
            ("Texas", "EASTMAN"),
        ),
        ({"street1": "2665 PRISON RD # 1", "zip": "75851"}, None),
        # A display name in two towns: the ZIP decides, or nothing does.
        ({"street1": "TWIN TOWN UNIT", "zip": "76100"}, ("Texas", "TWINTOWN SOUTH")),
        ({"street1": "TWIN TOWN UNIT", "zip": "76200"}, None),
    ],
)
def test_unit_matching_by_display_name(coded_matcher, address, unit):
    """A label's lines match a unit's key name or display name; units that
    are one place give their representative."""
    assert coded_matcher.by_address(address) == unit


def test_since_run_fetches_stored_labels_the_listing_misses(sessions):
    """A label bought on or after --since from a shipment created before it
    isn't listed (the listing goes by creation time), so it is fetched by id
    and its unit matched again; labels the listing returned aren't."""
    with sessions() as session:
        add_shipment(session, "2026-01-01T01:00", kind="bulk")
        session.commit()
        add_unit(session, "GAMMA CODE", street1="Gamma Unit", zipcode="77002")
        session.commit()
    sync(FakeEasyPost([shipment(2, ago(3))]), sessions)  # a newer stored label
    straddling = shipment(
        1,
        utc("2025-12-31T23:00"),
        name="ATTN: MAILROOM STAFF",
        street1="300 New Rd",
        street2="GAMMA UNIT",
        zip="77002",
    )
    with sessions() as session:
        stored_id = session.scalars(
            sa.select(models.Shipment.easypost_id).where(
                models.Shipment.easypost_id != "shp_00000002"
            )
        ).one()
        straddling["id"] = stored_id
        straddling["postage_label"]["created_at"] = "2026-01-01T01:00:00Z"
    fake = FakeEasyPost([straddling, shipment(2, ago(3))])
    summary = sync(fake, sessions, since=datetime.date(2026, 1, 1))
    assert summary.fetched_by_id == 1
    assert [path for path, _ in fake.calls if path.startswith("shipments/")] == [
        f"shipments/{stored_id}"
    ]
    assert stored(sessions, stored_id).unit_name == "GAMMA CODE"
    assert "1 fetched by id" in summary.line()


def seed_stale_link(sessions) -> None:
    """A stored label bought since 2026-01-01 that EasyPost no longer has,
    and a link a rebuild would remove."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-01-02")
        label = add_shipment(session, "2026-01-05T15:00", kind="other")
        session.flush()
        session.add(
            models.PackageShipment(
                inmate_jurisdiction="Texas",
                inmate_id=40000001,
                request_index=0,
                shipment_id=label.id,
                basis="reference",
            )
        )
        session.commit()


@pytest.mark.parametrize("failures", [[], [500] * easypost_client.RETRIES])
def test_incomplete_since_run_fails_without_rebuilding_links(sessions, logs, failures):
    """A --since run that fails part-way (listing, or fetching a stored
    label by id) exits 1, says to re-run it, and leaves the links alone."""
    seed_stale_link(sessions)
    fake = FakeEasyPost(failures=failures)
    client = EasyPost(KEY, urlopen=fake, sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        assert (
            shipment_sync.main(
                ["--since", "2026-01-01"], sessions=sessions, client=client
            )
            == 1
        )
    assert (
        "re-evaluation since 2026-01-01 is incomplete and the links were not "
        "rebuilt; re-run with --since 2026-01-01"
    ) in logs.text
    with sessions() as session:
        assert session.query(models.PackageShipment).count() == 1


def test_failed_daily_run_says_nothing_of_a_re_evaluation(sessions, logs):
    """Without --since, a failure is just the failure line."""
    client = EasyPost(
        KEY,
        urlopen=FakeEasyPost(failures=[500] * easypost_client.RETRIES),
        sleep=lambda _: None,
    )
    with logs.at_level(logging.INFO):
        assert shipment_sync.main([], sessions=sessions, client=client) == 1
    assert "shipment sync failed" in logs.text
    assert "re-evaluation" not in logs.text


def test_street_without_a_number_is_not_a_name(api_db):
    """A unit whose only line is a street with no house number has no
    display name, so a label on that street elsewhere doesn't match it."""
    with api_db() as session:
        matcher = UnitMatcher(
            [add_unit(session, "ROADSIDE", street1="Highway 190", zipcode="77000")]
        )
    assert matcher.by_address({"street1": "HIGHWAY 190", "zip": "77999"}) is None
    assert matcher.by_address({"street1": "Roadside", "zip": "77999"}) == (
        "Texas",
        "ROADSIDE",
    )


def test_same_named_units_at_different_streets_stay_ambiguous(api_db):
    """Two units with one display name and ZIP at different streets aren't
    one place, so the name alone doesn't tell them apart."""
    with api_db() as session:
        matcher = UnitMatcher(
            [
                add_unit(session, "FIRST", street1="Twin Unit", street2="100 First Rd"),
                add_unit(
                    session, "SECOND", street1="Twin Unit", street2="200 Second Rd"
                ),
            ]
        )
    assert matcher.by_address({"street1": "TWIN UNIT", "zip": "77000"}) is None


@pytest.fixture
def evidence_matcher(api_db):
    """A Texas unit, a Federal unit elsewhere, and units with malformed or
    empty ZIPs (invented names and addresses)."""
    with api_db() as session:
        units = [
            add_unit(
                session,
                "ALPHA CODE",
                street1="Alpha Unit",
                street2="100 Example Rd",
                zipcode="77000",
            ),
            add_unit(
                session,
                "DELTA CODE",
                jurisdiction="Federal",
                street1="Delta Unit",
                street2="5 Federal Way",
                zipcode="77600",
                shipping_method="Federal Tub",
            ),
            add_unit(session, "BADZIP", street1="300 Odd Rd", zipcode="77-000"),
            add_unit(session, "NOZIP", street1="400 Blank Rd", zipcode=""),
            add_unit(session, "Doubled", street1="6 Texas Way", zipcode=""),
            add_unit(
                session,
                "Doubled",
                jurisdiction="Federal",
                street1="6 Federal Way",
                zipcode="77600",
                shipping_method="Federal Tub",
            ),
        ]
        return UnitMatcher(units)


@pytest.mark.parametrize(
    "address, unit",
    [
        # The street and ZIP say one unit, a name line another: unknown.
        (
            {"company": "DELTA UNIT", "street1": "100 EXAMPLE RD", "zip": "77000"},
            None,
        ),
        # Name and address agreeing, or a name with no address match, stand.
        (
            {"street1": "100 EXAMPLE RD", "street2": "ALPHA UNIT", "zip": "77000"},
            ("Texas", "ALPHA CODE"),
        ),
        (
            {"company": "DELTA UNIT", "street1": "1 Nowhere Rd", "zip": "77000"},
            ("Federal", "DELTA CODE"),
        ),
        # Only a well-formed ZIP counts, on either side.
        ({"street1": "100 EXAMPLE RD", "zip": "77-000"}, None),
        ({"street1": "100 EXAMPLE RD", "zip": "77000-1234"}, ("Texas", "ALPHA CODE")),
        ({"street1": "300 ODD RD", "zip": "77000"}, None),
        ({"street1": "300 ODD RD", "zip": "77-000"}, None),
        # Empty ZIPs never equal each other.
        ({"street1": "400 BLANK RD", "zip": ""}, None),
        ({"street1": "DOUBLED", "zip": ""}, None),
        # A name in both jurisdictions, the label's ZIP matching neither.
        ({"street1": "DOUBLED", "zip": "77000"}, None),
        ({"street1": "DOUBLED", "zip": "77600"}, ("Federal", "Doubled")),
    ],
)
def test_unit_matching_weighs_name_against_address(evidence_matcher, address, unit):
    """A name pointing away from the street and ZIP gives no unit, and only
    valid ZIPs are compared."""
    assert evidence_matcher.by_address(address) == unit


def test_relisting_updates_a_stored_labels_unit(sessions):
    """A label stored before its unit could be told gets the unit when it is
    listed again (as ``--since`` lists every label back to its date)."""
    fake = FakeEasyPost(
        [
            shipment(
                1,
                ago(3),
                name="ATTN: MAILROOM STAFF",
                street1="300 New Rd",
                street2="GAMMA UNIT",
                zip="77002",
            )
        ]
    )
    sync(fake, sessions)
    assert stored(sessions, "shp_00000001").unit_name is None
    with sessions() as session:
        add_unit(session, "GAMMA CODE", street1="Gamma Unit", zipcode="77002")
        session.commit()
    summary = sync(fake, sessions)
    assert summary.updated == 1
    assert stored(sessions, "shp_00000001").unit_name == "GAMMA CODE"


# --- listing -------------------------------------------------------------------


def test_month_windows_split_at_month_starts():
    """Windows are newest first and never cross a month start."""
    assert month_windows(utc("2026-01-20T00:00"), utc("2026-03-10T12:00")) == [
        (utc("2026-03-01T00:00"), utc("2026-03-10T12:00")),
        (utc("2026-02-01T00:00"), utc("2026-03-01T00:00")),
        (utc("2026-01-20T00:00"), utc("2026-02-01T00:00")),
    ]


def test_first_run_inserts_labels_and_events(sessions):
    """Labels, their kinds, destinations and events are stored."""
    individual = shipment(
        1,
        ago(3),
        [event(ago(3), "pre_transit"), event(ago(2), "in_transit")],
    )
    bulk = shipment(
        2, ago(4), name="ATTN: MAILROOM STAFF", street1="200 Other Way", zip="77001"
    )
    other = shipment(3, ago(5), name="Some Library", street1="1 Elsewhere St")
    tagged = shipment(
        4, ago(6), reference="ibp1:unit:Texas:beta", name="ATTN: MAILROOM STAFF"
    )
    not_bought = shipment(5, ago(6), selected_rate=None, tracking_code=None)
    fake = FakeEasyPost([individual, bulk, other, tagged, not_bought])

    summary = sync(fake, sessions)

    assert (summary.seen, summary.new, summary.updated) == (4, 4, 0)
    assert summary.events_added == 5
    row = stored(sessions, "shp_00000001")
    assert row.kind == "individual"
    assert row.inmate_number == "40000001"
    assert (row.unit_jurisdiction, row.unit_name) == ("Texas", "ALPHA")
    assert row.tracking_code == individual["tracking_code"]
    assert row.tracker_id == "trk_00000001"
    assert row.purchased_at == ago(3) + datetime.timedelta(seconds=4)
    assert (row.weight_oz, row.cost_cents, row.service) == (48.0, 557, "LibraryMail")
    assert (row.reference, row.refund_status, row.tracker_status) == (
        None,
        None,
        "in_transit",
    )
    assert row.synced_at == NOW
    assert [(e.status, e.occurrence) for e in row.events] == [
        ("pre_transit", 0),
        ("in_transit", 0),
    ]
    assert row.events[1].local_time is not None
    assert (row.events[1].city, row.events[1].state, row.events[1].zip) == (
        "EXAMPLETON",
        "TX",
        "77000",
    )
    assert stored(sessions, "shp_00000002").kind == "bulk"
    assert stored(sessions, "shp_00000002").unit_name == "BETA"
    assert stored(sessions, "shp_00000003").kind == "other"
    assert stored(sessions, "shp_00000003").unit_name is None
    tag_row = stored(sessions, "shp_00000004")
    assert (tag_row.kind, tag_row.unit_name) == ("bulk", "BETA")
    assert tag_row.reference == "ibp1:unit:Texas:beta"


def test_no_recipient_name_or_street_is_stored(sessions):
    """Neither the addressee's name nor any street line reaches the database."""
    fake = FakeEasyPost(
        [shipment(1, ago(3), street2="Apt 9 Example Wing", company="Example Co")]
    )
    sync(fake, sessions)
    text = repr(snapshot(sessions)).upper()
    for secret in (
        NAME,
        STREET,
        "100 EXAMPLE",
        "EXAMPLE WING",
        "EXAMPLE CO",
        "SOMEONE",
    ):
        assert secret.upper() not in text


def test_reference_that_is_not_a_tag_is_not_stored(sessions):
    """Free text in the reference (a name, say) is dropped; a tag is kept."""
    sentinel = "Sentinel Q. Personname"
    fake = FakeEasyPost(
        [
            shipment(1, ago(3), reference=sentinel),
            shipment(2, ago(3), reference=f"ibp1:req:{sentinel}"),
            shipment(3, ago(3), reference="ibp1:manual"),
        ]
    )
    sync(fake, sessions)
    assert stored(sessions, "shp_00000001").reference is None
    assert stored(sessions, "shp_00000002").reference is None
    assert stored(sessions, "shp_00000003").reference == "ibp1:manual"
    text = repr(snapshot(sessions)).upper()
    assert "SENTINEL" not in text and "PERSONNAME" not in text


def test_second_identical_run_changes_nothing(sessions):
    """Running again with the same answers writes nothing."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-03-01")
        add_inmate(session, 40000002, "ALPHA")
        add_request(session, 40000002, 0, "2026-03-01")
        session.commit()
    fake = FakeEasyPost(
        [
            shipment(
                1, ago(3), [event(ago(3), "pre_transit"), event(ago(1), "delivered")]
            ),
            shipment(2, ago(4), name="ATTN: MAILROOM STAFF"),
        ]
    )
    first = sync(fake, sessions)
    # one inmate's label by number, the other inmate's request by session
    assert first.links == 2
    before = snapshot(sessions)

    again = sync(fake, sessions, now=NOW + datetime.timedelta(hours=1))

    assert (again.seen, again.new, again.updated, again.events_added) == (2, 0, 0, 0)
    assert again.links == 2
    assert snapshot(sessions) == before


def test_later_run_adds_only_new_events(sessions):
    """New events are added; stored ones are not duplicated."""
    events = [event(ago(3), "pre_transit"), event(ago(2), "in_transit")]
    fake = FakeEasyPost([shipment(1, ago(3), list(events))])
    sync(fake, sessions)

    fake.shipments = [
        shipment(
            1, ago(3), [*events, event(ago(1), "delivered", "arrived_at_destination")]
        )
    ]
    later = NOW + DAY
    summary = sync(fake, sessions, now=later)

    assert (summary.new, summary.updated, summary.events_added) == (0, 1, 1)
    row = stored(sessions, "shp_00000001")
    assert [e.status for e in row.events] == ["pre_transit", "in_transit", "delivered"]
    assert row.tracker_status == "delivered"
    assert row.synced_at == later


def test_every_event_is_kept_including_returns_and_repeats(sessions):
    """Delivered then returned keeps both; two deliveries are both kept; an
    identical repeat is kept as a second occurrence."""
    delivered = event(ago(5), "delivered", "arrived_at_destination")
    fake = FakeEasyPost(
        [
            shipment(
                1,
                ago(9),
                [
                    event(ago(9), "pre_transit"),
                    delivered,
                    event(ago(4), "failure", "refused", city=None),
                    event(ago(3), "return_to_sender", "return"),
                ],
            ),
            shipment(
                2,
                ago(9),
                [
                    delivered,
                    event(ago(2), "delivered", "arrived_at_destination"),
                    dict(delivered),
                ],
            ),
        ]
    )
    sync(fake, sessions)
    sync(fake, sessions)

    returned = stored(sessions, "shp_00000001")
    assert [e.status for e in returned.events] == [
        "pre_transit",
        "delivered",
        "failure",
        "return_to_sender",
    ]
    assert returned.events[2].city is None
    twice = stored(sessions, "shp_00000002")
    assert [(e.status, e.occurrence) for e in twice.events] == [
        ("delivered", 0),
        ("delivered", 1),
        ("delivered", 0),
    ]


def test_refunded_never_scanned_label_is_stored_without_links(sessions):
    """A refunded label USPS never scanned is kept but links nothing."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-03-01")
        session.commit()
    fake = FakeEasyPost([shipment(1, ago(3), refund_status="refunded")])
    summary = sync(fake, sessions)
    assert summary.new == 1 and summary.links == 0
    assert stored(sessions, "shp_00000001").refund_status == "refunded"


def test_tagged_label_links_by_reference(sessions):
    """A label tagged with a request code is stored with it and linked."""
    with sessions() as session:
        add_inmate(session, 12345, "ALPHA")
        add_request(session, 12345, 2, "2026-03-01")
        session.commit()
    fake = FakeEasyPost(
        [shipment(1, ago(3), reference="ibp1:req:TEX-00012345-2", name="Testy Example")]
    )
    summary = sync(fake, sessions)
    row = stored(sessions, "shp_00000001")
    assert (
        row.kind,
        row.request_jurisdiction,
        row.request_inmate_id,
        row.request_index,
    ) == ("individual", "Texas", 12345, 2)
    assert row.inmate_number is None
    assert summary.links == 1


def test_429_then_success_is_retried(sessions):
    """Rate limits and server errors are retried with backoff."""
    fake = FakeEasyPost([shipment(1, ago(3))], failures=[(429, 7), 503])
    sleeps: list[float] = []
    summary = sync(fake, sessions, sleeps=sleeps)
    assert summary.new == 1
    assert 7.0 in sleeps  # Retry-After
    assert 2.0 in sleeps  # exponential backoff on the second attempt


def test_client_gives_up_after_retries():
    """A request still failing after every retry raises ApiError."""
    fake = FakeEasyPost(failures=[500] * shipment_sync.RETRIES)
    client = EasyPost(KEY, urlopen=fake, sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError, match="HTTP 500"):
        client.get("shipments", {"start_datetime": "a", "end_datetime": "b"})
    assert len(fake.calls) == shipment_sync.RETRIES


SENTINEL = "Sentinel Q. Personname"


def failing_urlopen(status: int, body: bytes):
    """An opener that answers every request with HTTP ``status`` and ``body``."""

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        raise urllib.error.HTTPError(
            request.full_url, status, "error", email.message.Message(), io.BytesIO(body)
        )

    return urlopen


@pytest.mark.parametrize(
    "body, message",
    [
        (
            json.dumps(
                {
                    "error": {
                        "code": "ADDRESS.VERIFY.FAILURE",
                        "message": f"Address for {SENTINEL} not found",
                        "errors": [{"field": "street1", "message": SENTINEL}],
                    }
                }
            ).encode(),
            "GET shipments: HTTP 422 ADDRESS.VERIFY.FAILURE",
        ),
        (f"<html>{SENTINEL}</html>".encode(), "GET shipments: HTTP 422"),
        (
            json.dumps({"error": {"code": SENTINEL}}).encode(),
            "GET shipments: HTTP 422 other",
        ),
        # A code that looks like one but isn't known is not logged.
        (
            json.dumps({"error": {"code": "Sentinel.Personname"}}).encode(),
            "GET shipments: HTTP 422 other",
        ),
        (json.dumps({"error": {"code": 7}}).encode(), "GET shipments: HTTP 422 other"),
        (json.dumps([SENTINEL]).encode(), "GET shipments: HTTP 422"),
    ],
)
def test_http_error_keeps_only_status_and_code(body, message):
    """An EasyPost error is its status and a known error code, never its
    body or an unknown code."""
    client = EasyPost(KEY, urlopen=failing_urlopen(422, body), sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError) as raised:
        client.get("shipments")
    assert str(raised.value) == message


def test_failed_run_logs_no_easypost_body(sessions, logs):
    """A run failing on an EasyPost error logs no part of its body."""
    body = json.dumps({"error": {"code": SENTINEL, "message": SENTINEL}}).encode()
    client = EasyPost(KEY, urlopen=failing_urlopen(400, body), sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        assert shipment_sync.main([], sessions=sessions, client=client) == 1
    assert "shipment sync failed: GET shipments: HTTP 400 other" in logs.text
    assert "Sentinel" not in logs.text and "Personname" not in logs.text


def sqlite_error(*statements: str) -> sqlite3.Error:
    """The error SQLite raises running ``statements``, carrying its code."""
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
    except sqlite3.Error as error:
        return error
    finally:
        connection.close()
    raise AssertionError("no error")


@pytest.mark.parametrize(
    "error, logged",
    [
        (
            sa.exc.IntegrityError(
                f"INSERT INTO shipments (reference) VALUES ('{SENTINEL}')",
                {"reference": SENTINEL},
                sqlite_error(
                    "CREATE TABLE t (x UNIQUE)",
                    "INSERT INTO t VALUES (1)",
                    "INSERT INTO t VALUES (1)",
                ),
            ),
            "IntegrityError: SQLITE_CONSTRAINT_UNIQUE",
        ),
        # The driver's message itself can name a value.
        (
            sa.exc.OperationalError(
                "SELECT 1",
                (SENTINEL,),
                sqlite_error('SELECT * FROM "Sentinel Q. Personname"'),
            ),
            "OperationalError: SQLITE_ERROR",
        ),
        # A driver error without SQLite's code is its class alone.
        (
            sa.exc.OperationalError(
                "SELECT 1", (), sqlite3.OperationalError(f"locked by {SENTINEL}")
            ),
            "OperationalError",
        ),
        (
            sa.exc.StatementError(
                f"bad value {SENTINEL}", f"SELECT '{SENTINEL}'", {"x": SENTINEL}, None
            ),
            "StatementError",
        ),
        (sa.exc.InvalidRequestError(SENTINEL), "InvalidRequestError"),
    ],
)
def test_database_failure_logs_no_statement_or_values(
    sessions, logs, monkeypatch, error, logged
):
    """A database error is logged as its class (and SQLite's error code
    name), without the statement, its parameters or the driver's message."""

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(shipment_sync, "run", fail)
    monkeypatch.setattr(shipment_sync, "relink", fail)
    client = EasyPost(KEY, urlopen=FakeEasyPost(), sleep=lambda _: None)
    for argv in ([], ["--rebuild-links"]):
        logs.clear()
        with logs.at_level(logging.INFO):
            assert shipment_sync.main(argv, sessions=sessions, client=client) == 1
        assert f"shipment sync failed: {logged}" in logs.text
        assert "Sentinel" not in logs.text and "Personname" not in logs.text


def source_line(module, text: str) -> int:
    """The number of the line of ``module``'s source holding ``text``."""
    lines = Path(module.__file__).read_text(encoding="utf-8").splitlines()
    (number,) = [n for n, line in enumerate(lines, 1) if text in line]
    return number


def assert_one_safe_line(logs, capsys, logged: str) -> None:
    """The failure is logged as ``logged``, with no traceback or sentinel."""
    output = capsys.readouterr()
    assert f"shipment sync failed: {logged}" in logs.text
    assert all(record.exc_info is None for record in logs.records)
    for text in (logs.text, output.out, output.err):
        assert "Sentinel" not in text and "Personname" not in text
        assert "Traceback" not in text


def test_unexpected_error_logs_class_and_place_only(
    sessions, logs, capsys, monkeypatch
):
    """A bug fails the run with one line: the error's class and where IBP's
    code raised it, not its message."""

    def fail(*args, **kwargs):
        raise ValueError(SENTINEL)

    monkeypatch.setattr(label_reading, "cents", fail)
    now = datetime.datetime.now(datetime.timezone.utc)
    client = EasyPost(
        KEY, urlopen=FakeEasyPost([shipment(1, now - DAY)]), sleep=lambda _: None
    )
    with logs.at_level(logging.INFO):
        assert shipment_sync.main([], sessions=sessions, client=client) == 1
    line = source_line(label_reading, "cost_cents=cents(")
    assert_one_safe_line(logs, capsys, f"ValueError at ibp/label_reading.py:{line}")


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": f"shp_{SENTINEL}"},
        {"id": None},
        {"id": 7},
        {"tracking_code": f"9400 {SENTINEL}"},
        {"tracker": {"id": f"trk_{SENTINEL}", "tracking_details": []}},
        {"tracker": [SENTINEL]},
        {"to_address": SENTINEL},
        {"name": ["#12345678"]},
        {"parcel": {"weight": SENTINEL}},
        {"parcel": {"weight": "nan"}},
        {"reference": {"tag": SENTINEL}},
        {"postage_label": {"created_at": SENTINEL}},
        {
            "tracker": {
                "id": "trk_00000001",
                "tracking_details": [{"datetime": SENTINEL}],
            }
        },
        {"tracker": {"id": "trk_00000001", "tracking_details": [SENTINEL]}},
    ],
)
def test_malformed_label_is_skipped_counted_and_never_logged(
    sessions, logs, capsys, overrides
):
    """A listed label not of EasyPost's shape is not stored, is counted as
    skipped, and none of its values reach the log; the run goes on, and
    fails at the end (it didn't get every label)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    fake = FakeEasyPost(
        [shipment(1, now - 2 * DAY, **overrides), shipment(2, now - DAY)]
    )
    client = EasyPost(KEY, urlopen=fake, sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        assert shipment_sync.main([], sessions=sessions, client=client) == 1
    assert "1 labels seen, 1 new" in logs.text and "1 skipped" in logs.text
    with sessions() as session:
        assert [row.easypost_id for row in session.query(models.Shipment)] == [
            "shp_00000002"
        ]
    output = capsys.readouterr()
    for text in (logs.text, output.out, output.err):
        assert "Sentinel" not in text and "Personname" not in text


def test_malformed_tracker_is_a_poll_failure_and_never_logged(sessions, logs):
    """A polled tracker not of EasyPost's shape changes nothing and is a
    failed poll; its values are not logged."""
    with sessions() as session:
        label = old_label(session, 50, [(48, "in_transit")])
        session.commit()
        tracker_id = label.tracker_id
    fake = FakeEasyPost(
        trackers={
            tracker_id: {"id": tracker_id, "status": [SENTINEL], "tracking_details": []}
        }
    )
    with logs.at_level(logging.INFO):
        summary = sync(fake, sessions)
    assert (summary.polled, summary.poll_failures) == (0, 1)
    assert "tracker status is not text" in logs.text
    assert "Sentinel" not in logs.text


@pytest.mark.parametrize(
    "page",
    [
        {"shipments": {"0": SENTINEL}, "has_more": False},
        {"shipments": [{"id": SENTINEL}], "has_more": True},
        {"shipments": [SENTINEL], "has_more": True},
        # Either would end the listing early as if it were complete.
        {"has_more": False},
        {"shipments": [], "has_more": "true"},
        {"shipments": [{"id": "shp_00000001"}], "has_more": 1},
    ],
)
def test_page_not_of_easyposts_shape_is_an_api_error(page):
    """A listing page that is not a list, or can't be paged on, fails the
    listing as an ApiError naming no value."""
    client = EasyPost(
        KEY,
        urlopen=lambda request, timeout: io.BytesIO(json.dumps(page).encode()),
        sleep=lambda _: None,
    )
    with pytest.raises(shipment_sync.ApiError) as raised:
        list(shipment_sync.list_window(client, ago(2), NOW))
    assert str(raised.value).startswith("GET shipments: ")
    assert "Sentinel" not in str(raised.value)


def test_stored_id_of_another_shape_is_not_logged(sessions, logs):
    """A stored label whose id isn't of EasyPost's shape (stored before ids
    were checked) is not named in a tracker warning."""
    with sessions() as session:
        old_label(session, 50, [(48, "in_transit")], easypost_id=f"shp_{SENTINEL}")
        session.commit()
    with logs.at_level(logging.INFO):
        sync(FakeEasyPost(), sessions)
    assert "tracker for a label with an id of another shape not fetched" in (logs.text)
    assert "Sentinel" not in logs.text


def test_unexpected_error_rebuilding_links_logs_class_and_place_only(
    sessions, logs, capsys, monkeypatch
):
    """--rebuild-links fails the same way, at the innermost frame in ibp/."""

    def fail(*args, **kwargs):
        raise KeyError(SENTINEL)

    monkeypatch.setattr(shipment_links, "_unit_session_links", fail)
    with logs.at_level(logging.INFO):
        assert shipment_sync.main(["--rebuild-links"], sessions=sessions) == 1
    line = source_line(shipment_links, "_unit_session_links(session, shipments")
    assert_one_safe_line(logs, capsys, f"KeyError at ibp/shipment_links.py:{line}")


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_interrupt_and_exit_are_not_caught(sessions, monkeypatch, error):
    """Ctrl-C and sys.exit still end the run as they would anyway."""

    def stop(*args, **kwargs):
        raise error

    monkeypatch.setattr(shipment_sync, "relink", stop)
    with pytest.raises(error):
        shipment_sync.main(["--rebuild-links"], sessions=sessions)


def test_request_failures_name_the_operation_only(logs):
    """Retry warnings and errors name a fixed operation, not the request's
    id or query string."""
    client = EasyPost(KEY, urlopen=failing_urlopen(503, b""), sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        with pytest.raises(shipment_sync.ApiError) as raised:
            client.get("trackers/trk_Sentinel")
        with pytest.raises(shipment_sync.ApiError):
            client.get("shipments", {"start_datetime": "Sentinel"})
    assert str(raised.value) == "GET tracker: HTTP 503"
    assert "GET tracker: HTTP 503, retrying" in logs.text
    assert "GET shipments: HTTP 503, retrying" in logs.text
    assert "Sentinel" not in logs.text and "trk_" not in logs.text


@pytest.mark.parametrize(
    "reason, logged",
    [
        (f"cannot reach {SENTINEL}", "URLError"),
        (
            ConnectionRefusedError(111, f"refused by {SENTINEL}"),
            "ConnectionRefusedError",
        ),
    ],
)
def test_network_error_is_its_reasons_class(logs, reason, logged):
    """A network error is logged as its reason's class, not its text."""

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        raise urllib.error.URLError(reason)

    client = EasyPost(KEY, urlopen=urlopen, sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        with pytest.raises(shipment_sync.ApiError) as raised:
            client.get("shipments")
    assert str(raised.value) == f"GET shipments: {logged}"
    assert f"GET shipments: {logged}, retrying" in logs.text
    assert "Sentinel" not in logs.text


def test_job_engine_hides_parameters(monkeypatch, tmp_path):
    """The job's own engine keeps parameters out of its error messages."""
    monkeypatch.setattr(
        db_bootstrap, "get_sync_database_url", lambda: f"sqlite:///{tmp_path}/x.db"
    )
    with shipment_sync.build_sessions()() as session:
        engine = session.get_bind()
    try:
        assert engine.hide_parameters
    finally:
        engine.dispose()


class BrokenBody(io.BytesIO):
    """A response body whose reading fails with ``error``."""

    def __init__(self, error: Exception):
        super().__init__()
        self.error = error

    def read(self, *args):  # pylint: disable=unused-argument
        raise self.error


@pytest.mark.parametrize(
    "answer",
    [
        lambda: BrokenBody(TimeoutError("timed out")),
        lambda: BrokenBody(http.client.IncompleteRead(b'{"shipments": [', 500)),
        lambda: BrokenBody(ConnectionResetError(104, "reset")),
        lambda: io.BytesIO(b'{"shipments": ['),
        lambda: io.BytesIO(b"<html>maintenance</html>"),
        lambda: io.BytesIO(b"\xff\xfe"),
    ],
)
def test_unreadable_response_is_retried_then_an_api_error(answer):
    """A timeout, truncated body or invalid JSON is retried, then raises
    ApiError."""
    calls = []

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        calls.append(request)
        return answer()

    client = EasyPost(KEY, urlopen=urlopen, sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError, match="reading response"):
        client.get("shipments")
    assert len(calls) == shipment_sync.RETRIES


def test_unreadable_response_succeeds_on_retry():
    """One bad read then a good answer gives the good answer."""
    answers = [BrokenBody(TimeoutError("timed out")), io.BytesIO(b'{"ok": true}')]
    client = EasyPost(
        KEY, urlopen=lambda request, timeout: answers.pop(0), sleep=lambda _: None
    )
    assert client.get("shipments") == {"ok": True}


def test_unreadable_tracker_is_a_poll_failure_and_the_run_goes_on(sessions):
    """A tracker whose answer can't be read is counted as a failed poll; the
    other trackers are still polled and the links still rebuilt."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-01-20")
        bad = old_label(session, 50, [(48, "in_transit")])
        good = old_label(session, 49, [(48, "in_transit")], inmate_number="40000001")
        session.commit()
        bad_path, good_tracker = f"trackers/{bad.tracker_id}", good.tracker_id
    fake = FakeEasyPost(
        trackers={good_tracker: {"id": good_tracker, "status": "delivered"}}
    )

    def urlopen(request, timeout):
        if request.full_url.endswith(bad_path):
            return io.BytesIO(b'{"id": "trk_')
        return fake(request, timeout)

    client = EasyPost(KEY, urlopen=urlopen, sleep=lambda _: None)
    summary = shipment_sync.run(client, sessions, now=NOW)
    assert (summary.polled, summary.poll_failures, summary.links) == (1, 1, 1)


def test_client_paces_requests():
    """Every request after the first waits PACE_SECONDS."""
    fake = FakeEasyPost()
    sleeps: list[float] = []
    client = EasyPost(KEY, urlopen=fake, sleep=sleeps.append)
    for _ in range(3):
        client.get("shipments", {"start_datetime": "a", "end_datetime": "b"})
    assert sleeps == [easypost_client.PACE_SECONDS] * 2


def test_paging_across_more_than_one_page(sessions, monkeypatch):
    """Pages are followed with before_id until has_more is false."""
    monkeypatch.setattr(shipment_sync, "PAGE_SIZE", 2)
    fake = FakeEasyPost(
        [shipment(n, ago(1) - n * datetime.timedelta(minutes=5)) for n in range(1, 6)]
    )
    summary = sync(fake, sessions)
    assert summary.new == 5
    befores = [params.get("before_id") for path, params in fake.calls]
    # Three pages of the March window, then the (empty) February one.
    assert befores == [None, "shp_00000002", "shp_00000004", None]


def test_backfill_walks_month_windows(sessions):
    """--since lists every month back to that date, and labels in each."""
    fake = FakeEasyPost(
        [shipment(1, utc("2025-12-20T10:00")), shipment(2, utc("2026-02-14T10:00"))]
    )
    summary = sync(fake, sessions, since=datetime.date(2025, 12, 15))
    assert summary.new == 2
    assert fake.listing_windows() == [
        ("2026-03-01T00:00:00Z", "2026-03-10T12:05:01Z"),
        ("2026-02-01T00:00:00Z", "2026-03-01T00:00:01Z"),
        ("2026-01-01T00:00:00Z", "2026-02-01T00:00:01Z"),
        ("2025-12-15T00:00:00Z", "2026-01-01T00:00:01Z"),
    ]


def test_normal_run_lists_the_last_35_days(sessions):
    """Without --since only the recent window is listed."""
    fake = FakeEasyPost([shipment(1, ago(40)), shipment(2, ago(30))])
    summary = sync(fake, sessions)
    assert summary.new == 1
    assert fake.listing_windows() == [
        ("2026-03-01T00:00:00Z", "2026-03-10T12:05:01Z"),
        ("2026-02-03T12:00:00Z", "2026-03-01T00:00:01Z"),
    ]


@pytest.mark.parametrize(
    "newest, first_start",
    [
        (None, "2026-02-03T12:00:00Z"),  # nothing stored: the last 35 days
        (10, "2026-02-03T12:00:00Z"),  # stored recently: the last 35 days
        (50, "2026-01-18T12:00:00Z"),  # an outage: a day before the newest
    ],
)
def test_listing_resumes_from_the_newest_stored_label(sessions, newest, first_start):
    """After an outage longer than the window, listing starts a day before
    the newest stored label, so nothing bought meanwhile is missed."""
    if newest is not None:
        with sessions() as session:
            old_label(session, newest + 5, [])
            old_label(session, newest, [])
            session.commit()
    fake = FakeEasyPost([shipment(1, ago(40))])
    summary = sync(fake, sessions)
    assert fake.listing_windows()[-1][0] == first_start
    assert summary.new == (1 if newest == 50 else 0)


# --- polling older labels ------------------------------------------------------


def old_label(session, days: float, events, **columns) -> models.Shipment:
    """A stored label bought ``days`` before NOW with (days ago, status)
    events."""
    return add_shipment(
        session,
        ago(days).strftime("%Y-%m-%dT%H:%M"),
        events=tuple(
            (ago(when).strftime("%Y-%m-%dT%H:%M"), status) for when, status in events
        ),
        **columns,
    )


def test_is_finished_rules(api_db):
    """Finished: 90 days old, never mailed, returned, or delivered 60 days ago."""
    with api_db() as session:
        cases = {
            "old": old_label(session, 91, [(90, "in_transit")]),
            "refunded": old_label(
                session, 40, [(40, "pre_transit")], refund_status="refunded"
            ),
            "returned": old_label(session, 40, [(38, "return_to_sender")]),
            "settled": old_label(session, 75, [(74, "in_transit"), (61, "delivered")]),
            "recent_delivery": old_label(session, 75, [(59, "delivered")]),
            "in_transit": old_label(session, 50, [(48, "in_transit")]),
            "refunded_scanned": old_label(
                session, 40, [(39, "in_transit")], refund_status="refunded"
            ),
        }
        session.commit()
        finished = {name: is_finished(row, NOW) for name, row in cases.items()}
    assert finished == {
        "old": True,
        "refunded": True,
        "returned": True,
        "settled": True,
        "recent_delivery": False,
        "in_transit": False,
        "refunded_scanned": False,
    }


def test_unfinished_older_labels_are_polled(sessions):
    """Labels older than the window are fetched on their own until finished."""
    with sessions() as session:
        due = old_label(session, 50, [(48, "in_transit")])
        old_label(session, 80, [(75, "delivered")])  # finished
        old_label(session, 45, [(44, "return_to_sender")])  # finished
        untracked = old_label(session, 50, [(48, "in_transit")])
        untracked.tracker_id = None
        session.commit()
        due_id, tracker_id = due.easypost_id, due.tracker_id
    tracker = {
        "id": tracker_id,
        "status": "delivered",
        "tracking_details": [
            event(ago(48), "in_transit"),
            event(ago(20), "delivered", "arrived_at_destination"),
        ],
    }
    fake = FakeEasyPost(trackers={tracker_id: tracker})
    summary = sync(fake, sessions)

    assert [path for path, _ in fake.calls if path.startswith("trackers/")] == [
        f"trackers/{tracker_id}"
    ]
    assert (summary.polled, summary.poll_failures, summary.events_added) == (1, 0, 2)
    row = stored(sessions, due_id)
    assert row.tracker_status == "delivered"
    assert [e.status for e in row.events] == ["in_transit", "in_transit", "delivered"]

    again = sync(fake, sessions)
    assert (again.polled, again.events_added, again.updated) == (1, 0, 0)


def test_failed_tracker_poll_is_counted_and_others_continue(sessions):
    """A tracker EasyPost won't return doesn't stop the run, which fails."""
    with sessions() as session:
        old_label(session, 50, [(48, "in_transit")])
        session.commit()
    summary = sync(FakeEasyPost(), sessions)
    assert (summary.polled, summary.poll_failures) == (0, 1)


def test_unfetched_tracker_warning_names_no_tracker_id(sessions, logs):
    """The warning for a tracker not fetched names the label's opaque
    EasyPost id and the operation, not the tracker's path."""
    with sessions() as session:
        label = old_label(session, 50, [(48, "in_transit")])
        session.commit()
        easypost_id, tracker_id = label.easypost_id, label.tracker_id
    with logs.at_level(logging.INFO):
        sync(FakeEasyPost(), sessions)
    assert f"tracker for {easypost_id} not fetched: GET tracker: HTTP 404" in (
        logs.text
    )
    assert tracker_id not in logs.text


# --- the command line ----------------------------------------------------------


def test_missing_key_fails_clearly(sessions, monkeypatch, logs, tmp_path):
    """No key, an unreadable key file and a test key each fail with a message."""
    monkeypatch.delenv("EASYPOST_API_KEY", raising=False)
    monkeypatch.delenv("EASYPOST_API_KEY_FILE", raising=False)
    with logs.at_level(logging.ERROR):
        assert shipment_sync.main([], sessions=sessions) == 2
    assert "no EasyPost key" in logs.text

    logs.clear()
    missing = tmp_path / "missing-key"
    with logs.at_level(logging.ERROR):
        assert shipment_sync.main(["--key-file", str(missing)], sessions=sessions) == 2
    assert f"cannot read key file {missing}" in logs.text

    logs.clear()
    monkeypatch.setenv("EASYPOST_API_KEY", "EZTK" + "0" * 28)
    with logs.at_level(logging.ERROR):
        assert shipment_sync.main([], sessions=sessions) == 2
    assert "not a production EasyPost key" in logs.text


def test_key_is_read_from_file_or_environment(monkeypatch, tmp_path):
    """--key-file wins, then EASYPOST_API_KEY_FILE, then EASYPOST_API_KEY."""
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("EASYPOST_API_KEY", "EZAKfromenv")
    monkeypatch.delenv("EASYPOST_API_KEY_FILE", raising=False)
    assert shipment_sync.read_key(str(key_file)) == KEY
    assert shipment_sync.read_key(None) == "EZAKfromenv"
    monkeypatch.setenv("EASYPOST_API_KEY_FILE", str(key_file))
    assert shipment_sync.read_key(None) == KEY


def test_main_logs_a_summary_line(sessions, logs):
    """A run logs one line of counts, and no name or address."""
    now = datetime.datetime.now(datetime.timezone.utc)
    fake = FakeEasyPost([shipment(1, now - DAY)])
    client = EasyPost(KEY, urlopen=fake, sleep=lambda _: None)
    with logs.at_level(logging.INFO, logger="ibp.shipment_sync"):
        assert shipment_sync.main([], sessions=sessions, client=client) == 0
    assert (
        "shipment sync: 1 labels seen, 1 new, 0 updated, 0 fetched by id, "
        "0 skipped, 1 events added, "
        "0 trackers polled (0 failed), 0 links built"
    ) in logs.text
    assert NAME not in logs.text and STREET not in logs.text


def test_main_fails_when_a_poll_fails(sessions):
    """An unfetched tracker makes the run exit 1, for Healthchecks."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as session:
        add_shipment(session, (now - 50 * DAY).strftime("%Y-%m-%dT%H:%M"))
        session.commit()
    client = EasyPost(KEY, urlopen=FakeEasyPost(), sleep=lambda _: None)
    assert shipment_sync.main([], sessions=sessions, client=client) == 1


def test_main_rebuilds_links_without_a_key(sessions, monkeypatch):
    """--rebuild-links needs no key and makes no request."""
    monkeypatch.delenv("EASYPOST_API_KEY", raising=False)
    monkeypatch.delenv("EASYPOST_API_KEY_FILE", raising=False)
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-03-01")
        add_shipment(
            session,
            "2026-03-03T15:00",
            inmate_number="40000001",
            events=(("2026-03-05T15:00", "delivered"),),
        )
        session.commit()
    assert shipment_sync.main(["--rebuild-links"], sessions=sessions) == 0
    with sessions() as session:
        assert session.query(models.PackageShipment).count() == 1


# --- unexpected JSON shapes ----------------------------------------------------


@pytest.mark.parametrize("code", [[], {}, 7, None, True])
def test_error_code_of_any_shape_is_other_or_none(code):
    """A non-string error code is "other" (None for none), not a TypeError."""
    body = json.dumps({"error": {"code": code}}).encode()
    client = EasyPost(KEY, urlopen=failing_urlopen(404, body), sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError) as raised:
        client.get("trackers/trk_00000001")
    expected = "GET tracker: HTTP 404" + ("" if code is None else " other")
    assert str(raised.value) == expected


@pytest.mark.parametrize("body", [b"[]", b"null", b"7", b'"text"'])
def test_response_that_is_not_an_object_is_an_api_error(body):
    """A JSON body that is not an object is retried, then an ApiError."""
    client = EasyPost(
        KEY, urlopen=lambda request, timeout: io.BytesIO(body), sleep=lambda _: None
    )
    with pytest.raises(shipment_sync.ApiError, match="reading response"):
        client.get("shipments")


@pytest.mark.parametrize("value", ["nan", "-5", "inf", "soon"])
def test_odd_retry_after_falls_back_to_backoff(value):
    """A Retry-After that is not a finite, non-negative number is ignored."""
    headers = email.message.Message()
    headers["Retry-After"] = value
    error = urllib.error.HTTPError("u", 429, "busy", headers, io.BytesIO(b""))
    assert easypost_client.retry_after(error) is None


def test_odd_error_code_does_not_stop_the_polling(sessions):
    """A tracker answering with a list for its error code is a failed poll;
    the next tracker is still polled."""
    with sessions() as session:
        first = old_label(session, 50, [(48, "in_transit")])
        second = old_label(session, 49, [(48, "in_transit")])
        session.commit()
        first_tracker, second_tracker = first.tracker_id, second.tracker_id

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        if request.full_url.endswith(first_tracker):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "error",
                email.message.Message(),
                io.BytesIO(b'{"error": {"code": []}}'),
            )
        if request.full_url.endswith(second_tracker):
            return io.BytesIO(
                json.dumps({"id": second_tracker, "status": "delivered"}).encode()
            )
        return io.BytesIO(b'{"shipments": [], "has_more": false}')

    client = EasyPost(KEY, urlopen=urlopen, sleep=lambda _: None)
    summary = shipment_sync.run(client, sessions, now=NOW)
    assert (summary.polled, summary.poll_failures) == (1, 1)


# --- conflicting destination evidence ------------------------------------------


@pytest.fixture
def conflict_matcher(api_db):
    """Texas units at a street and at a P.O. box, and a Federal unit
    elsewhere (invented names and addresses)."""
    with api_db() as session:
        return UnitMatcher(
            [
                add_unit(session, "ALPHA", street1="100 Example Rd", zipcode="77000"),
                add_unit(
                    session,
                    "BRADLEY",
                    street1="Bradley Unit",
                    street2="P.O. Box 9000",
                    zipcode="75600",
                ),
                add_unit(
                    session,
                    "DELTA CODE",
                    jurisdiction="Federal",
                    street1="Delta Unit",
                    street2="5 Federal Way",
                    zipcode="77600",
                    shipping_method="Federal Tub",
                ),
            ]
        )


@pytest.mark.parametrize(
    "address, destination",
    [
        # A Texas street, a Federal name.
        (
            {"company": "DELTA UNIT", "street1": "100 EXAMPLE RD", "zip": "77000"},
            (None, True),
        ),
        # A Texas P.O. box, a Federal name.
        (
            {"company": "DELTA UNIT", "street1": "PO BOX 9000", "zip": "75600"},
            (None, True),
        ),
        # A P.O. box alone is address evidence.
        ({"street1": "PO BOX 9000", "zip": "75600"}, (("Texas", "BRADLEY"), False)),
        ({"street1": "PO BOX 9001", "zip": "75600"}, (None, False)),
        # No evidence at all is not a conflict.
        ({"street1": "1 Nowhere Rd", "zip": "77000"}, (None, False)),
        (
            {"company": "BRADLEY UNIT", "street1": "PO BOX 9000", "zip": "75600"},
            (("Texas", "BRADLEY"), False),
        ),
    ],
)
def test_locate_tells_conflicting_evidence_from_none(
    conflict_matcher, address, destination
):
    """A name contradicted by the street or P.O. box is a conflict, which
    no evidence is not."""
    assert tuple(conflict_matcher.locate(address)) == destination


def test_label_with_conflicting_destination_links_no_one(sessions):
    """An untagged label to a Texas street naming a Federal facility is
    kind other, so its addressee's number links no request in either
    jurisdiction; listed again with clean evidence it is individual again."""
    with sessions() as session:
        add_unit(
            session,
            "DELTA CODE",
            jurisdiction="Federal",
            street1="Delta Unit",
            street2="5 Federal Way",
            zipcode="77600",
            shipping_method="Federal Tub",
        )
        add_inmate(session, 40000001, jurisdiction="Federal")
        add_request(
            session, 40000001, 0, ago(5).date().isoformat(), jurisdiction="Federal"
        )
        session.commit()
    conflicting = shipment(1, ago(3), company="DELTA UNIT")
    summary = sync(FakeEasyPost([conflicting]), sessions)
    row = stored(sessions, "shp_00000001")
    assert (row.kind, row.unit_name, row.inmate_number) == ("other", None, "40000001")
    assert summary.links == 0
    clean = shipment(1, ago(3))
    sync(FakeEasyPost([clean]), sessions)
    row = stored(sessions, "shp_00000001")
    assert (row.kind, row.unit_name) == ("individual", "ALPHA")


# --- a --since run publishes only a complete re-evaluation ---------------------


class AnswerById:
    """A FakeEasyPost whose ``GET shipments/{id}`` answers ``answer``."""

    def __init__(self, fake: FakeEasyPost, answer: dict):
        self.fake, self.answer = fake, answer

    def __call__(self, request, timeout):
        if "/v2/shipments/" in request.full_url:
            return io.BytesIO(json.dumps(self.answer).encode())
        return self.fake(request, timeout)


def stale_link_label(sessions) -> str:
    """The id of seed_stale_link's label."""
    seed_stale_link(sessions)
    with sessions() as session:
        return session.scalars(sa.select(models.Shipment.easypost_id)).one()


def run_since(sessions, logs, urlopen, since="2026-01-01") -> int:
    """main with --since against ``urlopen``."""
    client = EasyPost(KEY, urlopen=urlopen, sleep=lambda _: None)
    with logs.at_level(logging.INFO):
        return shipment_sync.main(["--since", since], sessions=sessions, client=client)


def assert_incomplete(sessions, logs, since="2026-01-01") -> None:
    """The run said to re-run it and left the one stale link alone."""
    assert (
        f"re-evaluation since {since} is incomplete and the links were not "
        f"rebuilt; re-run with --since {since}"
    ) in logs.text
    with sessions() as session:
        assert session.query(models.PackageShipment).count() == 1


@pytest.mark.parametrize(
    "answer, failure",
    [
        (lambda id_: shipment(9, ago(3), selected_rate=None), "answer has no label"),
        (
            lambda id_: shipment(9, ago(3), tracking_code=None),
            "answer has no label",
        ),
        (lambda id_: shipment(9, ago(3)), "answer is another shipment"),
        (
            lambda id_: {**shipment(9, ago(3)), "id": id_, "tracker": [SENTINEL]},
            "answer not of EasyPost's shape",
        ),
    ],
)
def test_since_run_with_an_unusable_answer_by_id_is_incomplete(
    sessions, logs, answer, failure
):
    """A fetch by id answered with no label, another shipment or a shape
    not EasyPost's doesn't cover the stored label: the run fails without
    rebuilding the links."""
    easypost_id = stale_link_label(sessions)
    urlopen = AnswerById(FakeEasyPost(), answer(easypost_id))
    assert run_since(sessions, logs, urlopen) == 1
    assert f"shipment sync failed: GET shipment: {failure}" in logs.text
    assert "Sentinel" not in logs.text
    assert_incomplete(sessions, logs)


def test_since_run_with_a_failed_tracker_poll_rebuilds_no_links(sessions, logs):
    """A tracker that can't be fetched in a --since run leaves the links as
    they were: they are rebuilt only after every step succeeded."""
    seed_stale_link(sessions)
    today = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as session:
        add_shipment(
            session,
            (today - 50 * DAY).strftime("%Y-%m-%dT%H:%M"),
            events=(((today - 49 * DAY).strftime("%Y-%m-%dT%H:%M"), "in_transit"),),
        )
        session.commit()
    since = (today - 10 * DAY).date().isoformat()
    assert run_since(sessions, logs, FakeEasyPost(), since=since) == 1
    assert "shipment sync failed: 1 trackers not fetched" in logs.text
    assert_incomplete(sessions, logs, since=since)


# --- street lines as address evidence ------------------------------------------


@pytest.fixture
def street_matcher(api_db):
    """River Unit at 100 Main Street; FM units at 100 FM 1 in another ZIP,
    one alone and two sharing the number (invented)."""
    with api_db() as session:
        return UnitMatcher(
            [
                add_unit(
                    session,
                    "RIVER",
                    street1="River Unit",
                    street2="100 Main Street",
                    zipcode="77000",
                ),
                add_unit(
                    session,
                    "LONE",
                    street1="Lone Unit",
                    street2="100 FM 1",
                    zipcode="77100",
                ),
                add_unit(
                    session,
                    "PAIR A",
                    street1="Pair A Unit",
                    street2="200 FM 2",
                    zipcode="77200",
                ),
                add_unit(
                    session,
                    "PAIR B",
                    street1="Pair B Unit",
                    street2="200 FM 2 Annex",
                    zipcode="77200",
                ),
            ]
        )


@pytest.mark.parametrize(
    "address, destination",
    [
        # The named unit's number on a street that isn't provably its own,
        # with no other unit at that number: the name stands.
        (
            {"company": "RIVER UNIT", "street1": "100 OAK LN", "zip": "77000"},
            (("Texas", "RIVER"), False),
        ),
        # Without a name, another street at the number is no evidence.
        ({"street1": "100 OAK LN", "zip": "77000"}, (None, False)),
        # The same street, in USPS spelling, confirms it.
        (
            {"street1": "100 MAIN ST", "street2": "RIVER UNIT", "zip": "77000"},
            (("Texas", "RIVER"), False),
        ),
        ({"street1": "100 MAIN ST", "zip": "77000"}, (("Texas", "RIVER"), False)),
        # A name with no street line stands.
        ({"company": "RIVER UNIT", "zip": "77000"}, (("Texas", "RIVER"), False)),
        # Can't tell, and the number is the unit's alone: the name stands.
        (
            {"company": "LONE UNIT", "street1": "100 FM 1 SPUR", "zip": "77100"},
            (("Texas", "LONE"), False),
        ),
        ({"street1": "100 FM 1 SPUR", "zip": "77100"}, (("Texas", "LONE"), False)),
        # Can't tell, at a number two places share: the name decides.
        (
            {"company": "PAIR A UNIT", "street1": "200 FM 2 SPUR", "zip": "77200"},
            (("Texas", "PAIR A"), False),
        ),
        ({"street1": "200 FM 2 SPUR", "zip": "77200"}, (None, False)),
    ],
)
def test_street_line_confirms_or_contradicts_the_name(
    street_matcher, address, destination
):
    """The label's street line is compared with the named unit's at the
    same number, not just the number."""
    assert tuple(street_matcher.locate(address)) == destination


# Our street and USPS's where normalisation can't prove them the same (a
# renamed street, another suffix), from real rows in made-up form.
DRIFTED = [
    ("1400 Industrial Blvd", "1400 E MLK JR INDUSTRIAL BLVD"),
    ("8602 Peach Street", "8602 PEACH AVE"),
    ("295 I.H. 45 North", "295 IH 45 N"),
    ("23001 IH 35", "23001 INTERSTATE 35"),
]


@pytest.mark.parametrize(
    "ours, theirs, provable",
    [*((o, t, True) for o, t in RESPELLED), *((o, t, False) for o, t in DRIFTED)],
)
@pytest.mark.parametrize("street_line", ["street2", "street1"])
def test_real_shapes_still_resolve(api_db, ours, theirs, provable, street_line):
    """Units keyed by a short code with the display name in street1 and the
    street in street2 (or the street alone in street1), and labels in the
    verified shape (USPS street in street1, name in street2) and the
    unverified one (name in street1, a street in street2), all resolve; a
    label with no name does too when its street is provably ours."""
    with api_db() as session:
        if street_line == "street2":
            row = add_unit(
                session, "SHORT", street1="Sample Unit", street2=ours, zipcode="77300"
            )
        else:
            row = add_unit(session, "SHORT", street1=ours, zipcode="77300")
        decoy = add_unit(
            session,
            "DECOY",
            street1="Decoy Unit",
            street2="5 Other Rd",
            zipcode="77300",
        )
        matcher = UnitMatcher([row, decoy])
    name = "SAMPLE UNIT" if street_line == "street2" else "SHORT"
    verified = {"street1": theirs, "street2": name, "zip": "77300-1234"}
    unverified = {"street1": name, "street2": ours, "zip": "77300"}
    unverified_usps = {"street1": name, "street2": theirs, "zip": "77300"}
    street_only = {"street1": theirs, "zip": "77300"}
    shapes = [verified, unverified, unverified_usps] + (
        [street_only] if provable else []
    )
    for address in shapes:
        assert tuple(matcher.locate(address)) == (("Texas", "SHORT"), False), address


def test_since_run_that_skipped_a_label_rebuilds_no_links(sessions, logs):
    """A malformed listed label in a --since run makes it incomplete before
    the links are rebuilt."""
    seed_stale_link(sessions)
    with sessions() as session:
        stored_id = session.scalars(sa.select(models.Shipment.easypost_id)).one()
    good = {**shipment(1, utc("2026-01-05T15:00")), "id": stored_id}
    fake = FakeEasyPost(
        [good, shipment(2, utc("2026-01-06T15:00"), tracker=[SENTINEL])]
    )
    assert run_since(sessions, logs, fake) == 1
    assert "shipment sync failed: 1 listed labels skipped" in logs.text
    assert_incomplete(sessions, logs)


def test_since_run_with_a_label_it_cannot_store_rebuilds_no_links(
    sessions, logs, monkeypatch
):
    """A label that reads but can't be stored fails a --since run before
    the links are rebuilt."""
    seed_stale_link(sessions)

    def broken(*args, **kwargs):
        raise sa.exc.OperationalError("INSERT", {}, sqlite_error("SELECT * FROM nope"))

    monkeypatch.setattr(shipment_sync, "store_label", broken)
    fake = FakeEasyPost([shipment(1, utc("2026-01-06T15:00"))])
    assert run_since(sessions, logs, fake) == 1
    assert "shipment sync failed: OperationalError: SQLITE_ERROR" in logs.text
    assert_incomplete(sessions, logs)


# --- a tracker answer never takes evidence away --------------------------------


def scanned_old_box(sessions) -> tuple[str, str]:
    """A box bought 20 days before NOW whose only scan evidence is its
    stored in_transit status, linked to a request; its (label, tracker) ids."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, ago(25).date().isoformat())
        box = add_shipment(
            session,
            ago(20).strftime("%Y-%m-%dT%H:%M"),
            kind="bulk",
            unit_jurisdiction="Texas",
            unit_name="ALPHA",
            tracker_status="in_transit",
            events=((ago(20).strftime("%Y-%m-%dT%H:%M"), "pre_transit"),),
        )
        session.commit()
        return box.easypost_id, box.tracker_id


@pytest.mark.parametrize(
    "answer",
    [
        lambda tracker_id: {},
        lambda tracker_id: {"id": tracker_id},
        lambda tracker_id: {"id": tracker_id, "status": None},
        lambda tracker_id: {"id": tracker_id, "status": "Sentinel"},
        lambda tracker_id: {"id": "trk_99999999", "status": "unknown"},
    ],
)
def test_unusable_tracker_answer_changes_nothing(sessions, answer):
    """An empty or wrong tracker answer is a failed poll: the stored status
    stays, and so does the bulk link it is the evidence for."""
    easypost_id, tracker_id = scanned_old_box(sessions)
    assert shipment_sync.relink(sessions, NOW) == 1
    fake = FakeEasyPost(trackers={tracker_id: answer(tracker_id)})
    summary = sync(fake, sessions)
    assert (summary.polled, summary.poll_failures) == (0, 1)
    row = stored(sessions, easypost_id)
    assert row.tracker_status == "in_transit"
    assert len(row.events) == 1
    assert summary.links == 1


def test_listing_without_tracker_status_keeps_the_stored_one(sessions):
    """A listed label whose tracker gives no status (or no tracker) keeps
    the status and tracker stored for it, and every event."""
    first = shipment(
        1, ago(3), [event(ago(3), "pre_transit"), event(ago(2), "in_transit")]
    )
    sync(FakeEasyPost([first]), sessions)
    bare = shipment(1, ago(3), [], tracker_status=None)
    bare["tracker"] = {"id": "trk_00000001", "status": None, "tracking_details": []}
    sync(FakeEasyPost([bare]), sessions)
    row = stored(sessions, "shp_00000001")
    assert (row.tracker_id, row.tracker_status) == ("trk_00000001", "in_transit")
    assert len(row.events) == 2
    no_tracker = {**shipment(1, ago(3)), "tracker": None}
    sync(FakeEasyPost([no_tracker]), sessions)
    row = stored(sessions, "shp_00000001")
    assert (row.tracker_id, row.tracker_status) == ("trk_00000001", "in_transit")
    assert len(row.events) == 2


def test_listed_tracker_status_must_be_easyposts(sessions):
    """A listed label whose tracker status isn't one of EasyPost's is
    skipped, not stored."""
    odd = shipment(1, ago(3), tracker_status="Sentinel")
    summary = sync(FakeEasyPost([odd]), sessions)
    assert (summary.seen, summary.skipped) == (0, 1)


# --- a listing ends only when EasyPost says so ---------------------------------


def paged(pages):
    """An opener answering successive listing requests with ``pages``."""
    answers = list(pages)

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        return io.BytesIO(json.dumps(answers.pop(0)).encode())

    return urlopen


@pytest.mark.parametrize(
    "pages, message",
    [
        # has_more missing on a page with shipments.
        ([{"shipments": [shipment(1, ago(1))]}], "page not of EasyPost's shape"),
        # An empty page that says more follow.
        ([{"shipments": [], "has_more": True}], "can't be paged on"),
        # A continuation whose last shipment repeats the previous page's.
        (
            [
                {"shipments": [shipment(2, ago(1))], "has_more": True},
                {"shipments": [shipment(2, ago(1))], "has_more": True},
            ],
            "paging made no progress",
        ),
    ],
)
def test_listing_that_cannot_be_trusted_to_be_complete_fails(pages, message):
    """A page that would end the listing early, or never end it, is an
    ApiError, not the end."""
    client = EasyPost(KEY, urlopen=paged(pages), sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError, match=message):
        list(shipment_sync.list_window(client, ago(2), NOW))


def test_listing_has_a_page_limit(monkeypatch):
    """A window that keeps saying more follow fails after MAX_PAGES."""
    monkeypatch.setattr(shipment_sync, "MAX_PAGES", 3)
    pages = [
        {"shipments": [shipment(n, ago(1))], "has_more": True} for n in range(1, 5)
    ]
    client = EasyPost(KEY, urlopen=paged(pages), sleep=lambda _: None)
    with pytest.raises(shipment_sync.ApiError, match="more than 3 pages in a window"):
        list(shipment_sync.list_window(client, ago(2), NOW))


def test_truncated_backfill_fails(sessions, logs):
    """A --since backfill whose page drops has_more exits 1 without
    rebuilding links instead of ending early."""
    seed_stale_link(sessions)
    pages = [{"shipments": [shipment(1, ago(1))]}] + [
        {"shipments": [], "has_more": False}
    ] * 50
    assert run_since(sessions, logs, paged(pages)) == 1
    assert "page not of EasyPost's shape" in logs.text
    assert_incomplete(sessions, logs)


def test_street_of_another_unit_contradicts_the_name(api_db):
    """A label naming River Unit (at 100 Main Street) on 100 Oak Lane, where
    another unit is, conflicts: positive evidence for another place."""
    with api_db() as session:
        matcher = UnitMatcher(
            [
                add_unit(
                    session,
                    "RIVER",
                    street1="River Unit",
                    street2="100 Main Street",
                    zipcode="77000",
                ),
                add_unit(
                    session,
                    "OAK",
                    street1="Oak Unit",
                    street2="100 Oak Lane",
                    zipcode="77000",
                ),
            ]
        )
    address = {"company": "RIVER UNIT", "street1": "100 OAK LN", "zip": "77000"}
    assert tuple(matcher.locate(address)) == (None, True)
    address = {"street1": "100 OAK LN", "street2": "OAK UNIT", "zip": "77000"}
    assert tuple(matcher.locate(address)) == (("Texas", "OAK"), False)


@pytest.fixture
def drifted_matcher(api_db):
    """Units in the shapes of real rows whose labels USPS spells otherwise
    (invented names, numbers and ZIPs)."""
    with api_db() as session:
        units = [
            # Two codes, one place; the street was renamed since.
            add_unit(
                session,
                "GSC",
                street1="Gregory S. Sample Unit",
                street2="1400 Industrial Blvd",
                zipcode="78000",
            ),
            add_unit(
                session,
                "GSC WORK",
                street1="Gregory S. Sample Unit",
                street2="1400 Industrial Blvd ",
                zipcode="78000",
            ),
            add_unit(
                session,
                "HOLLY",
                street1="Holly Unit",
                street2="295 I.H. 45 North",
                zipcode="77300",
            ),
            # Two differently named units at one address.
            add_unit(
                session,
                "MONT",
                street1="Montfort Unit",
                street2="8602 Peach Street",
                zipcode="79400",
            ),
            add_unit(
                session,
                "WTH",
                street1="West Sample Hospital",
                street2="8602 Peach Street",
                zipcode="79400",
            ),
            add_unit(
                session,
                "KYLO",
                street1="Kylo Unit",
                street2="23001 IH 35",
                zipcode="78600",
            ),
        ]
        return UnitMatcher(units)


@pytest.mark.parametrize(
    "name, street, zipcode, unit",
    [
        ("GREGORY S SAMPLE UNIT", "1400 E MLK JR INDUSTRIAL BLVD", "78000", "GSC"),
        ("HOLLY UNIT", "295 IH 45 N", "77300", "HOLLY"),
        ("MONTFORT UNIT", "8602 PEACH AVE", "79400", "MONT"),
        ("WEST SAMPLE HOSPITAL", "8602 PEACH AVE", "79400", "WTH"),
        ("KYLO UNIT", "23001 INTERSTATE 35", "78600", "KYLO"),
    ],
)
@pytest.mark.parametrize("shape", ["verified", "unverified", "company"])
def test_named_unit_stands_through_spelling_drift(
    drifted_matcher, name, street, zipcode, unit, shape
):
    """A label naming a unit at its house number and ZIP resolves to it even
    when USPS's street isn't provably ours, if no other place has that
    street; the two codes of one place give its representative."""
    address = {
        "verified": {"street1": street, "street2": name, "zip": f"{zipcode}-1234"},
        "unverified": {"street1": name, "street2": street, "zip": zipcode},
        "company": {"company": name, "street1": street, "zip": zipcode},
    }[shape]
    assert tuple(drifted_matcher.locate(address)) == (("Texas", unit), False)


@pytest.mark.parametrize(
    "ours, theirs",
    [
        ("295 I.H. 45 North", "295 IH 45 N"),
        ("23001 IH 35", "23001 INTERSTATE 35"),
        ("23001 I-35", "23001 IH 35"),
        ("1400 Martin Luther King Jr Blvd", "1400 MLK BLVD"),
    ],
)
def test_interstate_and_mlk_are_spelled_one_way(ours, theirs):
    """Cheap, safe respellings the normaliser knows."""
    from ibp.unit_places import same_street  # pylint: disable=import-outside-toplevel

    assert same_street(ours, theirs) is True
