"""The EasyPost shipment sync, against a fake EasyPost with invented data."""

# pylint: disable=redefined-outer-name

import datetime
import logging

import pytest
import sqlalchemy as sa

from ibp import models, shipment_sync
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


def test_unit_matching_by_tag_and_address(api_db):
    """Tags match unit names in any case; addresses match on street and ZIP
    (street2 breaks a tie, and a remaining tie is no match)."""
    with api_db() as session:
        alpha = add_unit(session, "ALPHA", street1="1 Shared Rd", zipcode="77002")
        beta = add_unit(
            session, "BETA", street1="1 Shared Rd", street2="Bldg B", zipcode="77002"
        )
        gamma = add_unit(session, "GAMMA", street1="9 Twin Rd", zipcode="77003")
        delta = add_unit(session, "DELTA", street1="9 Twin Rd", zipcode="77003")
        matcher = UnitMatcher([alpha, beta, gamma, delta])
    assert matcher.by_tag("Texas", "alpha") == ("Texas", "ALPHA")
    assert matcher.by_tag("Federal", "ALPHA") is None
    shared = {"street1": "1 SHARED RD.", "zip": "77002-0001"}
    assert matcher.by_address({**shared, "street2": "BLDG. B"}) == ("Texas", "BETA")
    assert matcher.by_address(shared) == ("Texas", "ALPHA")
    assert matcher.by_address({"street1": "9 Twin Rd", "zip": "77003"}) is None
    assert matcher.by_address({"street1": "5 Nowhere", "zip": "77002"}) is None


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
    row = stored(sessions, "shp_0001")
    assert row.kind == "individual"
    assert row.inmate_number == "40000001"
    assert (row.unit_jurisdiction, row.unit_name) == ("Texas", "ALPHA")
    assert row.tracking_code == individual["tracking_code"]
    assert row.tracker_id == "trk_0001"
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
    assert stored(sessions, "shp_0002").kind == "bulk"
    assert stored(sessions, "shp_0002").unit_name == "BETA"
    assert stored(sessions, "shp_0003").kind == "other"
    assert stored(sessions, "shp_0003").unit_name is None
    tag_row = stored(sessions, "shp_0004")
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


def test_second_identical_run_changes_nothing(sessions):
    """Running again with the same answers writes nothing."""
    with sessions() as session:
        add_inmate(session, 40000001, "ALPHA")
        add_request(session, 40000001, 0, "2026-03-01")
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
    assert first.links == 2  # the label by inmate number, the box by session
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
    row = stored(sessions, "shp_0001")
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

    returned = stored(sessions, "shp_0001")
    assert [e.status for e in returned.events] == [
        "pre_transit",
        "delivered",
        "failure",
        "return_to_sender",
    ]
    assert returned.events[2].city is None
    twice = stored(sessions, "shp_0002")
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
    assert stored(sessions, "shp_0001").refund_status == "refunded"


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
    row = stored(sessions, "shp_0001")
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


def test_client_paces_requests():
    """Every request after the first waits PACE_SECONDS."""
    fake = FakeEasyPost()
    sleeps: list[float] = []
    client = EasyPost(KEY, urlopen=fake, sleep=sleeps.append)
    for _ in range(3):
        client.get("shipments", {"start_datetime": "a", "end_datetime": "b"})
    assert sleeps == [shipment_sync.PACE_SECONDS] * 2


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
    assert befores == [None, "shp_0002", "shp_0004", None]


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
        "shipment sync: 1 labels seen, 1 new, 0 updated, 1 events added, "
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
