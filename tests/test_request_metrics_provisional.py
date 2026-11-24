"""Recent postmark months of the request counts are marked provisional."""

import datetime

from ibp import clock, metrics, models

from .shipment_helpers import add_inmate, add_unit

NUMBER = 12345678
TODAY = datetime.date(2026, 11, 24)


def add_waits(session, postmarked: str, waits) -> None:
    """Requests postmarked on ``postmarked`` and processed ``waits`` days
    later, one inmate each."""
    day = datetime.date.fromisoformat(postmarked)
    for wait in waits:
        number = NUMBER + len(session.new)
        add_inmate(session, number, "ALPHA")
        session.add(
            models.Request(
                inmate_jurisdiction="Texas",
                inmate_id=number,
                index=0,
                date_processed=day + datetime.timedelta(days=wait),
                date_postmarked=day,
                action="Filled",
            )
        )


def response(client, monkeypatch, **params) -> dict:
    """The request metrics on ``TODAY``."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    reply = client.get("/metrics/requests", params=params)
    assert reply.status_code == 200
    return reply.json()


def test_settle_time_is_the_95th_percentile_wait(client, api_db, monkeypatch):
    """Waits of 1..100 days in a settled month give 96 days (95.05 rounded
    up); impossible waits and months too recent to have settled are left out."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_waits(session, "2026-03-10", range(1, 101))
        add_waits(session, "2026-03-10", [-5, 400])  # mistyped postmarks
        add_waits(session, "2026-07-20", [1] * 80)  # month ended 116 days ago
        add_waits(session, "2026-10-05", [3])
        session.commit()

    body = response(client, monkeypatch, date_range="all")

    assert body["settle_days"] == 96
    assert [(m["month"], m["provisional"]) for m in body["data"]] == [
        ("2026-03", False),
        ("2026-07", False),  # ended 116 days ago
        ("2026-10", True),  # ended 24 days ago
    ]


def test_settle_time_falls_back_with_few_requests(client, api_db, monkeypatch):
    """Under 50 settled requests the default applies, and a month is
    provisional until that many days after it ends."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_waits(session, "2026-03-10", [200] * 49)
        add_waits(session, "2026-08-31", [10])  # ended 85 days ago
        add_waits(session, "2026-09-30", [10])  # ended 55 days ago
        session.commit()

    body = response(client, monkeypatch, date_range="all")

    assert body["settle_days"] == metrics.DEFAULT_SETTLE_DAYS == 75
    assert [(m["month"], m["provisional"]) for m in body["data"]] == [
        ("2026-03", False),
        ("2026-08", False),
        ("2026-09", True),
    ]


def test_settle_time_ignores_the_filters(client, api_db, monkeypatch):
    """The same months are provisional whatever the page is filtered to."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_waits(session, "2026-03-10", range(1, 101))
        add_waits(session, "2026-09-10", [3])
        session.commit()

    for params in ({"action": "Tossed"}, {"jurisdiction": "Federal"}, {}):
        body = response(client, monkeypatch, date_range="6months", **params)
        assert body["settle_days"] == 96, params
    assert body["data"] == [
        {
            "month": "2026-09",
            "filled_count": 1,
            "tossed_count": 0,
            "total_count": 1,
            "provisional": True,
        }
    ]
