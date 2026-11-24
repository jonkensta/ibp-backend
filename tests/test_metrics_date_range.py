"""The metrics date-range presets end on today's date in the configured zone."""

import datetime

from ibp import clock, metrics

from .test_delivery_latency import seed_months


def test_presets_end_on_the_configured_zones_today(monkeypatch):
    """Late evening in Chicago is still that day, though UTC has moved on."""
    monkeypatch.setattr(clock, "today", lambda: datetime.date(2026, 3, 10))
    assert metrics.calculate_date_range("6months") == (
        datetime.date(2025, 9, 11),
        datetime.date(2026, 3, 10),
    )
    assert metrics.calculate_date_range("12months") == (
        datetime.date(2025, 3, 10),
        datetime.date(2026, 3, 10),
    )


def test_all_or_unknown_preset_has_no_range():
    """No preset, ``all`` or an unknown one leaves the range open."""
    for preset in (None, "", "all", "fortnight"):
        assert metrics.calculate_date_range(preset) == (None, None)


TODAY = datetime.date(2026, 3, 31)  # 6months starts 2025-10-02, 12months 2025-03-31


def test_explicit_bounds_win_over_the_preset(monkeypatch):
    """A given bound is kept; the preset fills only the one not given."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    jan, feb = datetime.date(2026, 1, 1), datetime.date(2026, 2, 28)
    six = datetime.date(2025, 10, 2)

    assert metrics.resolve_date_range("6months", jan) == (jan, TODAY)
    assert metrics.resolve_date_range("6months", None, feb) == (six, feb)
    assert metrics.resolve_date_range("12months", jan, feb) == (jan, feb)
    assert metrics.resolve_date_range("all", jan) == (jan, None)
    assert metrics.resolve_date_range("all", None, feb) == (None, feb)
    assert metrics.resolve_date_range(None, jan, feb) == (jan, feb)
    assert metrics.resolve_date_range("6months") == (six, TODAY)
    assert metrics.resolve_date_range("all") == (None, None)
    assert metrics.resolve_date_range(None) == (None, None)


# Filled requests postmarked 2025-10-05 and 2026-02-08 (no label),
# 2026-01-02 (no label), 2026-01-20 (in a box) and 2026-03-03 (own label).
ALL_MONTHS = ["2025-10", "2026-01", "2026-02", "2026-03"]
MIXED = [
    # (params, months listed, bounds reported)
    (
        {"date_range": "6months", "start_date": "2026-02-01"},
        ["2026-02", "2026-03"],
        ("2026-02-01", "2026-03-31"),
    ),
    (
        {"date_range": "6months", "end_date": "2026-01-31"},
        ["2025-10", "2026-01"],
        ("2025-10-02", "2026-01-31"),
    ),
    (
        {
            "date_range": "12months",
            "start_date": "2026-01-01",
            "end_date": "2026-02-28",
        },
        ["2026-01", "2026-02"],
        ("2026-01-01", "2026-02-28"),
    ),
    (
        {"date_range": "all", "start_date": "2025-01-01"},
        ALL_MONTHS,
        ("2025-01-01", None),
    ),
    (
        {"date_range": "6months", "end_date": "2025-06-30"},
        [],
        ("2025-10-02", "2025-06-30"),
    ),
]


def latency(client, **params) -> dict:
    """The delivery latency response for ``params``."""
    response = client.get("/metrics/delivery-latency", params=params)
    assert response.status_code == 200
    return response.json()


def request_metrics(client, **params) -> dict:
    """The request metrics response for ``params``."""
    response = client.get("/metrics/requests", params=params)
    assert response.status_code == 200
    return response.json()


def bounds(body: dict) -> tuple:
    """The bounds a response reports it used."""
    return body["filters_applied"]["start_date"], body["filters_applied"]["end_date"]


def test_delivery_latency_mixed_bounds(client, api_db, monkeypatch):
    """Explicit bounds win over the preset, and only an open start trims the
    months before the first linked one."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    seed_months(api_db)

    for params, months, used in MIXED:
        body = latency(client, **params)
        assert [m["month"] for m in body["data"]] == months, params
        assert bounds(body) == used, params
    # No start: the months before the first linked one (January) go.
    body = latency(client, date_range="all", end_date="2026-02-28")
    assert [m["month"] for m in body["data"]] == ["2026-01", "2026-02"]
    assert bounds(body) == (None, "2026-02-28")


def test_request_metrics_mixed_bounds(client, api_db, monkeypatch):
    """/metrics/requests resolves the bounds the same way."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    seed_months(api_db)

    for params, months, used in MIXED:
        body = request_metrics(client, **params)
        assert [m["month"] for m in body["data"]] == months, params
        assert bounds(body) == used, params
    body = request_metrics(client, date_range="all", end_date="2026-02-28")
    assert [m["month"] for m in body["data"]] == ALL_MONTHS[:3]
    assert bounds(body) == (None, "2026-02-28")


def test_the_pages_presets_are_unchanged(client, api_db, monkeypatch):
    """The metrics page sends a preset alone (with ``method`` or the request
    filters), which resolves as before."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    seed_months(api_db)

    for method in ("all", "individual", "bulk"):
        for preset in ("6months", "12months"):
            body = latency(client, method=method, date_range=preset)
            assert [m["month"] for m in body["data"]] == ALL_MONTHS
        body = latency(client, method=method, date_range="all")
        assert [m["month"] for m in body["data"]] == ALL_MONTHS[1:]
        assert bounds(body) == (None, None)
    assert bounds(latency(client, date_range="6months")) == ("2025-10-02", "2026-03-31")

    for preset, months in (("6months", ALL_MONTHS), ("12months", ALL_MONTHS)):
        body = request_metrics(
            client, date_range=preset, jurisdiction="All", action="All"
        )
        assert [m["month"] for m in body["data"]] == months
    body = request_metrics(client, date_range="all", action="Filled")
    assert [m["month"] for m in body["data"]] == ALL_MONTHS
    assert bounds(body) == (None, None)
