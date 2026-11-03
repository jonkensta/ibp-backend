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
    assert "release" not in warnings.inmate(_inmate(release))


def test_past_release_date_warns_released():
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    assert "marked as released" in warnings.inmate(_inmate(yesterday))["release"]


def test_far_release_date_has_no_warning():
    later = datetime.date.today() + datetime.timedelta(days=3650)
    assert "release" not in warnings.inmate(_inmate(later))
