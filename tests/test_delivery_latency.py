"""The postmark-to-delivery latency metric. All data is invented."""

import datetime
from zoneinfo import ZoneInfo

import pytest

from ibp import models
from ibp.delivery_latency import (
    Event,
    FilledRequest,
    LabelOutcome,
    Link,
    label_outcomes,
    percentile,
    request_delivery,
    summarize,
)
from ibp.schemas import DeliveryMethodEnum

from .shipment_helpers import add_inmate, add_request, add_shipment, add_unit, utc

CHICAGO = ZoneInfo("America/Chicago")
NUMBER = 12345678
KEY = ("Texas", NUMBER, 0)


def filled(
    postmarked: str = "2026-03-01", processed: str = "2026-03-05", key=KEY
) -> FilledRequest:
    """A Filled request postmarked and processed on the given dates."""
    return FilledRequest(
        key,
        datetime.date.fromisoformat(postmarked),
        datetime.date.fromisoformat(processed),
    )


def link(
    shipment_id: int,
    basis: str = "reference",
    purchased: str = "2026-03-06T18:00",
    key=KEY,
) -> Link:
    """A link of ``key`` to label ``shipment_id`` bought at ``purchased`` (UTC)."""
    return Link(key, shipment_id, basis, utc(purchased))


def event(shipment_id: int, when: str, status: str) -> Event:
    """A tracking event of label ``shipment_id`` at ``when`` (UTC)."""
    return Event(shipment_id, utc(when), status)


def only_month(requests, links, events, method=DeliveryMethodEnum.ALL):
    """The single month ``summarize`` returns, as a dict."""
    (month,) = summarize(requests, links, events, method, CHICAGO)
    return month.model_dump()


def test_one_label():
    """One delivered label: both bounds agree and the stages add up."""
    month = only_month(
        [filled()], [link(1)], [event(1, "2026-03-10T18:00", "delivered")]
    )

    quartiles = {"p25": 9.0, "median": 9.0, "p75": 9.0, "median_transit": 4.0}
    assert month == {
        "month": "2026-03",
        "filled": 1,
        "linked": 1,
        "delivered": 1,
        "returned": 0,
        "excluded": 0,
        "earliest": quartiles,
        "latest": quartiles,
        "median_handling": 4.0,
        "median_waiting": 1.0,
    }


def test_session_of_two_boxes_delivered_on_different_days():
    """A request in a two-box session takes the first and last box's dates."""
    links = [link(1, "unit_session"), link(2, "unit_session")]
    events = [
        event(1, "2026-03-10T18:00", "delivered"),
        event(2, "2026-03-12T18:00", "delivered"),
    ]

    month = only_month([filled()], links, events)

    assert month["delivered"] == 1
    assert month["earliest"] == {
        "p25": 9.0,
        "median": 9.0,
        "p75": 9.0,
        "median_transit": 4.0,
    }
    assert month["latest"] == {
        "p25": 11.0,
        "median": 11.0,
        "p75": 11.0,
        "median_transit": 6.0,
    }


def test_returned_box_is_not_a_delivery():
    """A label with only a return_to_sender event is returned."""
    month = only_month(
        [filled()], [link(1)], [event(1, "2026-03-10T18:00", "return_to_sender")]
    )

    assert (month["linked"], month["delivered"], month["returned"]) == (1, 0, 1)
    assert month["earliest"] is None and month["latest"] is None
    assert month["median_handling"] is None and month["median_waiting"] is None


def test_delivered_then_returned_counts_as_delivered():
    """A mailroom that sends a delivered package back still had it delivered."""
    events = [
        event(1, "2026-03-10T18:00", "delivered"),
        event(1, "2026-03-15T18:00", "return_to_sender"),
    ]

    month = only_month([filled()], [link(1)], events)

    assert (month["delivered"], month["returned"]) == (1, 0)
    assert month["latest"]["median"] == 9.0


@pytest.mark.parametrize("returned_at", ["2026-03-08T18:00", "2026-03-12T18:00"])
def test_return_to_sender_then_delivered_is_returned(returned_at):
    """A "delivery" at or after a return_to_sender is the box coming back."""
    events = [
        event(1, returned_at, "return_to_sender"),
        event(1, "2026-03-12T18:00", "delivered"),
    ]

    month = only_month([filled()], [link(1)], events)

    assert (month["delivered"], month["returned"]) == (0, 1)
    assert month["latest"] is None


def test_no_delivery_yet():
    """A linked label still in the mail counts as linked only, never as days."""
    month = only_month(
        [filled()], [link(1)], [event(1, "2026-03-07T18:00", "in_transit")]
    )

    assert (month["filled"], month["linked"]) == (1, 1)
    assert (month["delivered"], month["returned"], month["excluded"]) == (0, 0, 0)
    assert month["latest"] is None


def test_unlinked_request_counts_as_filled_only():
    """A Filled request with no link is in ``filled`` alone."""
    month = only_month([filled()], [], [])

    assert (month["filled"], month["linked"], month["delivered"]) == (1, 0, 0)
    assert month["latest"] is None


def test_one_box_returned_one_still_in_the_mail_is_pending():
    """A request is returned only when every linked label was."""
    links = [link(1, "unit_session"), link(2, "unit_session")]
    events = [event(1, "2026-03-10T18:00", "return_to_sender")]

    month = only_month([filled()], links, events)

    assert (month["linked"], month["delivered"], month["returned"]) == (1, 0, 0)


def test_one_box_returned_one_delivered_uses_the_delivered_box():
    """A returned box gives no date; the delivered one does."""
    links = [link(1, "unit_session"), link(2, "unit_session")]
    events = [
        event(1, "2026-03-08T18:00", "return_to_sender"),
        event(2, "2026-03-12T18:00", "delivered"),
    ]

    month = only_month([filled()], links, events)

    assert (month["delivered"], month["returned"]) == (1, 0)
    assert month["earliest"]["median"] == month["latest"]["median"] == 11.0


def test_negative_latency_is_excluded():
    """A postmark after the delivery is a bad postmark."""
    month = only_month(
        [filled(postmarked="2026-03-20")],
        [link(1)],
        [event(1, "2026-03-10T18:00", "delivered")],
    )

    assert (month["delivered"], month["excluded"]) == (0, 1)
    assert month["latest"] is None


def test_latency_over_a_year_is_excluded():
    """366 days is out; 365 days is in."""
    requests = [
        filled(postmarked="2025-03-09", key=("Texas", NUMBER, 0)),
        filled(postmarked="2025-03-10", key=("Texas", NUMBER, 1)),
    ]
    links = [
        link(1, key=("Texas", NUMBER, 0)),
        link(2, key=("Texas", NUMBER, 1)),
    ]
    events = [
        event(1, "2026-03-10T18:00", "delivered"),
        event(2, "2026-03-10T18:00", "delivered"),
    ]

    month = only_month(requests, links, events)

    assert (month["filled"], month["delivered"], month["excluded"]) == (2, 1, 1)
    assert month["latest"]["median"] == 365.0


def test_excluded_when_one_box_is_out_of_range():
    """A box delivered before the postmark excludes the request from both
    bounds."""
    links = [link(1, "unit_session"), link(2, "unit_session")]
    events = [
        event(1, "2026-02-27T18:00", "delivered"),
        event(2, "2026-03-03T18:00", "delivered"),
    ]

    month = only_month([filled(postmarked="2026-02-28")], links, events)

    assert (month["delivered"], month["excluded"]) == (0, 1)


def test_method_filter():
    """individual counts a request's own label, bulk a box; filled is all."""
    own, boxed = ("Texas", NUMBER, 0), ("Texas", NUMBER + 1, 0)
    requests = [filled(key=own), filled(key=boxed)]
    links = [
        link(1, "inmate_number", key=own),
        link(2, "unit_session", key=boxed),
    ]
    events = [
        event(1, "2026-03-10T18:00", "delivered"),
        event(2, "2026-03-14T18:00", "delivered"),
    ]

    everything = only_month(requests, links, events, DeliveryMethodEnum.ALL)
    individual = only_month(requests, links, events, DeliveryMethodEnum.INDIVIDUAL)
    bulk = only_month(requests, links, events, DeliveryMethodEnum.BULK)

    assert (everything["filled"], everything["delivered"]) == (2, 2)
    assert everything["latest"]["median"] == 11.0
    assert (individual["filled"], individual["linked"]) == (2, 1)
    assert individual["latest"]["median"] == 9.0
    assert (bulk["filled"], bulk["linked"]) == (2, 1)
    assert bulk["latest"]["median"] == 13.0


def test_reference_counts_as_individual():
    """A tagged label is a request's own label."""
    month = only_month(
        [filled()],
        [link(1, "reference")],
        [event(1, "2026-03-10T18:00", "delivered")],
        DeliveryMethodEnum.INDIVIDUAL,
    )

    assert month["delivered"] == 1


def test_local_dates_across_midnight_utc():
    """Purchase and delivery times after midnight UTC are the day before in
    Chicago."""
    month = only_month(
        [filled()],
        [link(1, purchased="2026-03-07T04:00")],
        [event(1, "2026-03-11T03:00", "delivered")],
    )

    # Bought 2026-03-06 and delivered 2026-03-10, local.
    assert month["latest"]["median"] == 9.0
    assert month["latest"]["median_transit"] == 4.0
    assert month["median_waiting"] == 1.0


def test_earliest_delivered_event_wins():
    """A label delivered twice is delivered on the first date."""
    outcomes = label_outcomes(
        [
            event(1, "2026-03-12T18:00", "delivered"),
            event(1, "2026-03-10T18:00", "delivered"),
            event(1, "2026-03-09T18:00", "out_for_delivery"),
        ],
        CHICAGO,
    )

    assert outcomes == {1: LabelOutcome(delivered=datetime.date(2026, 3, 10))}


def test_request_delivery_needs_a_link():
    """A request without links has no delivery to work out."""
    with pytest.raises(ValueError):
        request_delivery([], {}, CHICAGO)


def test_request_delivery_uses_the_earliest_label_purchase():
    """Waiting to ship ends at the first linked label's purchase."""
    delivery = request_delivery(
        [link(1, purchased="2026-03-08T18:00"), link(2, purchased="2026-03-06T18:00")],
        {1: LabelOutcome(delivered=datetime.date(2026, 3, 10))},
        CHICAGO,
    )

    assert delivery.status == "delivered"
    assert delivery.purchased == datetime.date(2026, 3, 6)


def test_quartiles_over_several_requests():
    """Percentiles interpolate linearly between ranks."""
    keys = [("Texas", NUMBER, index) for index in range(4)]
    days = ["2026-03-06", "2026-03-10", "2026-03-11", "2026-03-21"]
    month = only_month(
        [filled(key=key) for key in keys],
        [link(i, key=key) for i, key in enumerate(keys)],
        [event(i, f"{day}T18:00", "delivered") for i, day in enumerate(days)],
    )

    # Latencies 5, 9, 10 and 20 days.
    assert month["latest"]["p25"] == 8.0
    assert month["latest"]["median"] == 9.5
    assert month["latest"]["p75"] == 12.5


def test_months_are_by_postmark_and_sorted():
    """Each postmark month is listed, oldest first, linked or not."""
    requests = [
        filled(postmarked="2026-04-02", processed="2026-04-05", key=KEY),
        filled(postmarked="2026-02-27", key=("Texas", NUMBER, 1)),
    ]

    months = summarize(requests, [], [], DeliveryMethodEnum.ALL, CHICAGO)

    assert [m.month for m in months] == ["2026-02", "2026-04"]


def test_percentile():
    """One value is every percentile; empty input is an error."""
    assert percentile([7], 0.25) == 7
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([4, 1, 3, 2], 0.5) == 2.5
    with pytest.raises(ValueError):
        percentile([], 0.5)


def seed(api_db) -> None:
    """Three Filled requests postmarked 2026-02-22 (one with its own label,
    one in a two-box session, one unlinked), a Tossed one, and one Filled
    request postmarked in January with a label still in the mail."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        for offset in range(4):
            add_inmate(session, NUMBER + offset, "ALPHA")
        add_request(session, NUMBER, 0, "2026-03-01")
        add_request(session, NUMBER + 1, 0, "2026-03-01")
        add_request(session, NUMBER + 2, 0, "2026-03-01")
        add_request(session, NUMBER + 3, 0, "2026-03-01", action="Tossed")
        add_request(session, NUMBER, 1, "2026-01-20")
        own = add_shipment(
            session,
            "2026-03-02T18:00",
            events=(
                ("2026-03-03T18:00", "in_transit"),
                ("2026-03-05T18:00", "delivered"),
            ),
        )
        boxes = [
            add_shipment(
                session,
                "2026-03-03T18:00",
                kind="bulk",
                events=((delivered, "delivered"),),
            )
            for delivered in ("2026-03-06T18:00", "2026-03-09T18:00")
        ]
        mailed = add_shipment(
            session, "2026-01-21T18:00", events=(("2026-01-22T18:00", "in_transit"),)
        )
        session.flush()
        for (inmate_id, index), shipment, basis in [
            ((NUMBER, 0), own, "reference"),
            ((NUMBER + 1, 0), boxes[0], "unit_session"),
            ((NUMBER + 1, 0), boxes[1], "unit_session"),
            ((NUMBER, 1), mailed, "inmate_number"),
        ]:
            session.add(
                models.PackageShipment(
                    inmate_jurisdiction="Texas",
                    inmate_id=inmate_id,
                    request_index=index,
                    shipment_id=shipment.id,
                    basis=basis,
                )
            )
        session.commit()


def test_endpoint_shape(client, api_db):
    """The endpoint reads the database and lists each postmark month."""
    seed(api_db)

    response = client.get("/metrics/delivery-latency")

    assert response.status_code == 200
    body = response.json()
    assert body["filters_applied"] == {
        "method": "all",
        "date_range": None,
        "start_date": None,
        "end_date": None,
    }
    january, february = body["data"]
    assert january == {
        "month": "2026-01",
        "filled": 1,
        "linked": 1,
        "delivered": 0,
        "returned": 0,
        "excluded": 0,
        "earliest": None,
        "latest": None,
        "median_handling": None,
        "median_waiting": None,
    }
    # Postmarked 2026-02-22, processed 2026-03-01; delivered after 11 days
    # (own label) and after 12 or 15 days (the session).
    assert february == {
        "month": "2026-02",
        "filled": 3,
        "linked": 2,
        "delivered": 2,
        "returned": 0,
        "excluded": 0,
        "earliest": {"p25": 11.25, "median": 11.5, "p75": 11.75, "median_transit": 3.0},
        "latest": {"p25": 12.0, "median": 13.0, "p75": 14.0, "median_transit": 4.5},
        "median_handling": 7.0,
        "median_waiting": 1.5,
    }


def test_endpoint_method_and_dates(client, api_db):
    """``method`` and the postmark range narrow the result."""
    seed(api_db)

    bulk = client.get("/metrics/delivery-latency", params={"method": "bulk"})
    february = client.get(
        "/metrics/delivery-latency",
        params={"start_date": "2026-02-01", "end_date": "2026-02-28"},
    )
    bad = client.get("/metrics/delivery-latency", params={"method": "boxes"})

    assert bulk.status_code == 200
    _, month = bulk.json()["data"]
    assert (month["filled"], month["linked"], month["delivered"]) == (3, 1, 1)
    assert month["latest"]["median"] == 15.0
    assert [m["month"] for m in february.json()["data"]] == ["2026-02"]
    assert bad.status_code == 422


def test_endpoint_empty(client, api_db):  # pylint: disable=unused-argument
    """No requests, no months."""
    response = client.get("/metrics/delivery-latency")

    assert response.status_code == 200
    assert response.json()["data"] == []
