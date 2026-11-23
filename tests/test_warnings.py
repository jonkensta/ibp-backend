"""Tests for inmate warnings."""

import datetime
from types import SimpleNamespace

import pytest

from ibp import warnings


def _inmate(release):
    return SimpleNamespace(
        jurisdiction="Federal",
        id=12345678,
        release=release,
        datetime_fetched=datetime.datetime.now(datetime.timezone.utc),
    )


@pytest.mark.parametrize(
    "release", [None, "UNKNOWN", "LIFE", "NOT AVAILABLE", "DEATH ROW", "DECEASED"]
)
def test_non_date_release_has_no_release_warning(release):
    """A release given as text, not a date, has nothing to warn about."""
    assert "release" not in warnings.inmate(_inmate(release))


def test_past_release_date_warns_released():
    """A release date already passed marks the inmate as released."""
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    assert "marked as released" in warnings.inmate(_inmate(yesterday))["release"]


def test_far_release_date_has_no_warning():
    """A release date years away has no warning."""
    later = datetime.date.today() + datetime.timedelta(days=3650)
    assert "release" not in warnings.inmate(_inmate(later))


FUTURE_POSTMARK = "The postmark date is in the future."


def _days_ago(days):
    return datetime.date.today() - datetime.timedelta(days=days)


def _inmate_with_requests(*requests):
    return SimpleNamespace(
        requests=[
            SimpleNamespace(action=action, date_postmarked=postmarked)
            for action, postmarked in requests
        ]
    )


def test_future_postmark_warns_with_no_earlier_requests():
    """A future postmark warns even for an inmate with no requests."""
    tomorrow = _days_ago(-1)
    result = warnings.request(_inmate_with_requests(), tomorrow)
    assert result == {"postmarkdate": FUTURE_POSTMARK}


def test_future_postmark_wins_over_spacing_warning():
    """A future postmark replaces the spacing warning sharing its key."""
    # Without the future check this would warn "Only 1 days since last postmark."
    inmate_ = _inmate_with_requests(("Filled", _days_ago(0)))
    result = warnings.request(inmate_, _days_ago(-1))
    assert result == {"postmarkdate": FUTURE_POSTMARK}


def test_todays_postmark_is_not_in_the_future():
    """Today's date is not a future postmark."""
    assert not warnings.request(_inmate_with_requests(), _days_ago(0))


def test_past_postmark_with_no_earlier_requests_has_no_warning():
    """A past postmark with no filled requests has no warning."""
    assert not warnings.request(_inmate_with_requests(), _days_ago(1))


def test_past_postmark_keeps_spacing_warning():
    """A past postmark soon after the last filled one still warns."""
    inmate_ = _inmate_with_requests(("Filled", _days_ago(31)), ("Tossed", _days_ago(2)))
    result = warnings.request(inmate_, _days_ago(1))
    assert result == {"postmarkdate": "Only 30 days since last postmark."}


def test_past_postmark_before_last_filled_keeps_its_warning():
    """A postmark before the last filled one still warns."""
    inmate_ = _inmate_with_requests(("Filled", _days_ago(1)))
    result = warnings.request(inmate_, _days_ago(5))
    assert result == {
        "postmarkdate": "There is a request with a postmark after this one."
    }
