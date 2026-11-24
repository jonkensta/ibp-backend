"""The metrics date-range presets end on today's date in the configured zone."""

import datetime

from ibp import clock, metrics


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
