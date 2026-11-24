"""Linking requests to the labels they could have shipped under."""

# pylint: disable=redefined-outer-name

import pytest

from ibp import models
from ibp.shipment_links import is_scanned, local_date, never_mailed, rebuild_links

from .shipment_helpers import (
    add_inmate,
    add_request,
    add_shipment,
    add_unit,
    links,
    utc,
)

NUMBER = 12345678
DELIVERED = (("2026-03-12T16:00", "in_transit"), ("2026-03-14T16:00", "delivered"))


@pytest.fixture
def session(api_db):
    """A session on a fresh database."""
    with api_db() as session:
        yield session


def rebuild(session) -> set[tuple]:
    """Rebuild the links and return them."""
    rebuild_links(session)
    session.commit()
    return links(session)


def individual(session, purchased="2026-03-10T15:00", **columns):
    """An untagged individual label to inmate NUMBER, delivered."""
    columns.setdefault("inmate_number", f"{NUMBER:08d}")
    columns.setdefault("events", DELIVERED)
    return add_shipment(session, purchased, kind="individual", **columns)


def box(session, purchased, unit="ALPHA", jurisdiction="Texas", **columns):
    """A bulk box to a unit, delivered."""
    columns.setdefault("events", DELIVERED)
    return add_shipment(
        session,
        purchased,
        kind="bulk",
        unit_jurisdiction=jurisdiction,
        unit_name=unit,
        **columns,
    )


def test_local_date_uses_the_server_time_zone():
    """A label bought late in the evening (Central) is dated that evening."""
    assert str(local_date(utc("2026-03-11T03:00"))) == "2026-03-10"


def test_tagged_label_links_to_its_request(session):
    """A reference tag naming a request links it; one naming a request IBP
    lacks links nothing."""
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, "2026-03-01")
    tagged = individual(
        session,
        request_jurisdiction="Texas",
        request_inmate_id=NUMBER,
        request_index=0,
        reference=f"ibp1:req:TEX-{NUMBER:08d}-0",
    )
    individual(
        session,
        request_jurisdiction="Texas",
        request_inmate_id=NUMBER,
        request_index=9,
        reference=f"ibp1:req:TEX-{NUMBER:08d}-9",
    )
    session.commit()
    assert rebuild(session) == {
        ("Texas", NUMBER, 0, tagged.easypost_id, "reference"),
    }


def test_untagged_label_links_to_latest_qualifying_request(session):
    """The latest Filled request on or before the purchase date wins; a
    tossed one, a later one and one in another jurisdiction don't count."""
    add_inmate(session, NUMBER)
    add_inmate(session, NUMBER, jurisdiction="Federal")
    add_request(session, NUMBER, 0, "2026-01-20")
    add_request(session, NUMBER, 1, "2026-02-20")
    add_request(session, NUMBER, 2, "2026-03-09", action="Tossed")
    add_request(session, NUMBER, 3, "2026-03-11")
    add_unit(session, "ALPHA")
    label = individual(session, unit_jurisdiction="Texas", unit_name="ALPHA")
    add_request(session, NUMBER, 0, "2026-03-05", jurisdiction="Federal")
    session.commit()
    assert rebuild(session) == {
        ("Texas", NUMBER, 1, label.easypost_id, "inmate_number"),
    }


def test_untagged_label_links_request_processed_that_day(session):
    """A request processed on the purchase date (local) qualifies."""
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, "2026-03-10")
    # 03:00 UTC on the 11th is the evening of the 10th in Texas.
    label = individual(session, purchased="2026-03-11T03:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", NUMBER, 0, label.easypost_id, "inmate_number"),
    }


@pytest.mark.parametrize(
    "processed, linked", [("2026-01-09", True), ("2026-01-08", False)]
)
def test_untagged_label_respects_60_days(session, processed, linked):
    """A request processed more than 60 days before the label isn't linked."""
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, processed)
    label = individual(session)  # 2026-03-10, 60 days after 2026-01-09
    session.commit()
    expected = {("Texas", NUMBER, 0, label.easypost_id, "inmate_number")}
    assert rebuild(session) == (expected if linked else set())


def test_destination_tells_texas_from_federal(session):
    """The same number in both jurisdictions: the destination unit decides,
    and with no destination neither is linked."""
    add_unit(
        session, "FCI EXAMPLE", jurisdiction="Federal", shipping_method="Federal Tub"
    )
    add_inmate(session, NUMBER)
    add_inmate(session, NUMBER, jurisdiction="Federal")
    add_request(session, NUMBER, 0, "2026-03-01")
    add_request(session, NUMBER, 0, "2026-03-01", jurisdiction="Federal")
    federal = individual(session, unit_jurisdiction="Federal", unit_name="FCI EXAMPLE")
    individual(session)  # destination unknown: ambiguous
    session.commit()
    assert rebuild(session) == {
        ("Federal", NUMBER, 0, federal.easypost_id, "inmate_number"),
    }


def test_unknown_destination_links_when_only_one_jurisdiction_matches(session):
    """With no destination, a number only one jurisdiction has is linked."""
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, "2026-03-01")
    label = individual(session)
    session.commit()
    assert rebuild(session) == {
        ("Texas", NUMBER, 0, label.easypost_id, "inmate_number"),
    }


def test_refunded_never_scanned_label_gets_no_links(session):
    """A refunded label USPS never scanned was never mailed; a refunded one
    that was scanned did travel and is linked."""
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, "2026-03-01")
    unmailed = individual(
        session,
        refund_status="refunded",
        tracker_status="pre_transit",
        events=(("2026-03-10T15:01", "pre_transit"),),
    )
    mailed = individual(session, refund_status="refunded")
    session.commit()
    assert never_mailed(unmailed) and not never_mailed(mailed)
    assert rebuild(session) == {
        ("Texas", NUMBER, 0, mailed.easypost_id, "inmate_number"),
    }


def seed_box_unit(session, method="Box"):
    """Unit ALPHA with two inmates there."""
    add_unit(session, "ALPHA", shipping_method=method)
    add_inmate(session, 11111111, "ALPHA")
    add_inmate(session, 22222222, "ALPHA")


def test_bulk_session_with_one_box(session):
    """A request links to the one box of the first session after it."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-01")
    first = box(session, "2026-03-05T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, first.easypost_id, "unit_session"),
    }


def test_bulk_session_with_three_boxes_and_the_next_session(session):
    """Every box of a day's session is linked; a request processed after a
    session links to the next one, not the earlier one."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-01")
    add_request(session, 22222222, 0, "2026-03-06")
    add_request(session, 22222222, 1, "2026-03-13")  # after every session
    boxes = [
        box(session, "2026-03-05T15:00"),
        box(session, "2026-03-05T18:00"),
        box(session, "2026-03-06T03:00"),  # still the 5th in Texas
    ]
    later = box(session, "2026-03-12T15:00")
    box(session, "2026-03-05T16:00", unit="BETA")  # another unit
    session.commit()
    assert rebuild(session) == {
        *(("Texas", 11111111, 0, b.easypost_id, "unit_session") for b in boxes),
        ("Texas", 22222222, 0, later.easypost_id, "unit_session"),
    }


def test_bulk_session_on_the_processed_date_waits_for_the_next(session):
    """A request processed on a session's date links to the next session."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-05")
    box(session, "2026-03-05T20:00")  # the same (local) day: too soon
    next_day = box(session, "2026-03-06T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, next_day.easypost_id, "unit_session"),
    }


def test_bulk_session_only_on_the_processed_date_links_nothing(session):
    """With no later session, a request processed on a session's date
    links nothing."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-05")
    box(session, "2026-03-05T20:00")
    session.commit()
    assert not rebuild(session)


def test_bulk_session_more_than_28_days_later_is_not_linked(session):
    """A request more than 28 days before the next session isn't linked."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-02-04")
    box(session, "2026-03-05T15:00")
    session.commit()
    assert not rebuild(session)


def test_bulk_session_28_days_later_is_linked(session):
    """A session exactly 28 days after the processed date still links."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-02-05")
    late = box(session, "2026-03-05T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, late.easypost_id, "unit_session"),
    }


def test_box_to_a_unit_marked_individual_still_links(session):
    """Boxes do go to units whose shipping method is Individual."""
    seed_box_unit(session, method="Individual")
    add_request(session, 11111111, 0, "2026-03-01")
    sent = box(session, "2026-03-05T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, sent.easypost_id, "unit_session"),
    }


def seed_transfer(session, recorded):
    """Inmate 11111111, filled at ALPHA and since moved to BETA, with the
    request recording ``recorded``; a box to each unit after the fill."""
    add_unit(session, "ALPHA")
    add_unit(session, "BETA", street1="200 Other Way")
    add_inmate(session, 11111111, "BETA")
    add_request(session, 11111111, 0, "2026-03-01").unit_name = recorded
    old = box(session, "2026-03-05T15:00", unit="ALPHA")
    new = box(session, "2026-03-05T16:00", unit="BETA")
    session.commit()
    return old, new


def test_transferred_inmate_links_to_the_recorded_unit(session):
    """A request that recorded its unit links to that unit's box, not to
    the box of the unit its inmate is at now."""
    old, _ = seed_transfer(session, "ALPHA")
    assert rebuild(session) == {
        ("Texas", 11111111, 0, old.easypost_id, "unit_session"),
    }


def test_request_without_a_recorded_unit_follows_the_inmate(session):
    """A request with no recorded unit (filled before it was recorded) uses
    the inmate's unit as it is now."""
    _, new = seed_transfer(session, None)
    assert rebuild(session) == {
        ("Texas", 11111111, 0, new.easypost_id, "unit_session"),
    }


def test_recorded_unit_that_no_longer_exists_links_nothing(session):
    """A recorded unit IBP no longer has matches no box, and the request
    doesn't fall back to the inmate's current unit."""
    seed_transfer(session, "GONE")
    box(session, "2026-03-05T17:00", unit="GONE")
    session.commit()
    assert not rebuild(session)


def seed_one_place(session):
    """Units MAIN and CAMP, one place under two codes, and NEIGHBOR, another
    unit at the same street address; an inmate at each."""
    add_unit(session, "MAIN", street1="Main Unit", street2="9 Prison Road")
    add_unit(session, "CAMP", street1="MAIN UNIT", street2="9 Prison Rd.")
    add_unit(session, "NEIGHBOR", street1="Neighbor Unit", street2="9 Prison Road")
    add_inmate(session, 11111111, "MAIN")
    add_inmate(session, 22222222, "CAMP")
    add_inmate(session, 33333333, "NEIGHBOR")
    for inmate_id in (11111111, 22222222, 33333333):
        add_request(session, inmate_id, 0, "2026-03-01")


def test_box_to_one_place_serves_every_code(session):
    """A box stored under a place's representative links requests of inmates
    under either code, but not those of the other unit at its address."""
    seed_one_place(session)
    sent = box(session, "2026-03-05T15:00", unit="CAMP")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, sent.easypost_id, "unit_session"),
        ("Texas", 22222222, 0, sent.easypost_id, "unit_session"),
    }


def test_boxes_under_two_codes_of_one_place_are_one_session(session):
    """Boxes stored under different codes of one place on one date are one
    session; a request recording one code links to both."""
    seed_one_place(session)
    add_request(session, 33333333, 1, "2026-03-01").unit_name = "MAIN"
    first = box(session, "2026-03-05T15:00", unit="CAMP")
    second = box(session, "2026-03-05T16:00", unit="MAIN")
    session.commit()
    assert rebuild(session) == {
        (*request, shipment.easypost_id, "unit_session")
        for request in (
            ("Texas", 11111111, 0),
            ("Texas", 22222222, 0),
            ("Texas", 33333333, 1),
        )
        for shipment in (first, second)
    }


def test_request_with_its_own_label_gets_no_bulk_link(session):
    """A request linked to an individual label didn't ride in a box."""
    seed_box_unit(session)
    add_request(session, NUMBER, 0, "2026-03-01")
    own = individual(session, "2026-03-03T15:00")
    box(session, "2026-03-05T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", NUMBER, 0, own.easypost_id, "inmate_number"),
    }


def test_unmailed_box_is_not_a_session(session):
    """A refunded, never scanned box doesn't make a session."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-01")
    box(session, "2026-03-05T15:00", refund_status="refunded", events=())
    mailed = box(session, "2026-03-07T15:00")
    session.commit()
    assert rebuild(session) == {
        ("Texas", 11111111, 0, mailed.easypost_id, "unit_session"),
    }


def test_old_unscanned_box_is_not_a_session(session):
    """A box USPS never scanned doesn't make a session once it is a week
    old; until then it does, since its first scan may not be in yet."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-01")
    unscanned = box(
        session, "2026-03-05T15:00", events=(("2026-03-05T15:01", "pre_transit"),)
    )
    mailed = box(session, "2026-03-07T15:00")
    session.commit()
    expected = ("Texas", 11111111, 0, unscanned.easypost_id, "unit_session")
    rebuild_links(session, now=utc("2026-03-12T14:59"))
    assert expected in links(session)
    rebuild_links(session, now=utc("2026-03-12T15:00"))
    assert links(session) == {
        ("Texas", 11111111, 0, mailed.easypost_id, "unit_session"),
    }


@pytest.mark.parametrize(
    "statuses, scanned",
    [
        ((), False),
        (("pre_transit",), False),
        (("pre_transit", "error"), False),
        (("cancelled",), False),
        (("unknown", "in_transit"), True),
        (("error", "delivered"), True),
    ],
)
def test_is_scanned_ignores_error_and_cancelled(session, statuses, scanned):
    """``error`` and ``cancelled`` are no sign that USPS handled a package."""
    label = individual(
        session,
        tracker_status=statuses[-1] if statuses else None,
        events=tuple(
            (f"2026-03-1{n}T15:00", status) for n, status in enumerate(statuses)
        ),
    )
    assert is_scanned(label) is scanned


def test_rebuild_is_deterministic_and_idempotent(session):
    """Rebuilding gives the same rows every time and replaces stray ones."""
    seed_box_unit(session)
    add_request(session, 11111111, 0, "2026-03-01")
    add_inmate(session, NUMBER)
    add_request(session, NUMBER, 0, "2026-03-01")
    boxes = [box(session, "2026-03-05T15:00"), box(session, "2026-03-05T16:00")]
    individual(session)
    session.commit()

    first = rebuild(session)
    assert len(first) == 3
    assert rebuild(session) == first

    session.query(models.PackageShipment).delete()
    session.add(
        models.PackageShipment(
            inmate_jurisdiction="Texas",
            inmate_id=NUMBER,
            request_index=0,
            shipment_id=boxes[0].id,
            basis="reference",
        )
    )
    session.commit()
    assert rebuild(session) == first
    assert rebuild_links(session) == 3
