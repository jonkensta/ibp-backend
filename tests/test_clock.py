"""Today's date in the configured time zone."""

import configparser
import datetime

import pytest

from ibp import clock

# 9:30 pm in Chicago (CST, UTC-6) on 2026-01-15, already 2026-01-16 in UTC.
CHICAGO_EVENING = datetime.datetime(2026, 1, 16, 3, 30, tzinfo=datetime.timezone.utc)


def _config(text: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read_string(text)
    return config


def test_default_timezone_is_chicago():
    """sample.conf sets the server to US Central time."""
    assert clock.TIMEZONE.key == "America/Chicago"


def test_today_is_the_configured_zones_date():
    """An evening in Chicago is still that day, though UTC has moved on."""
    assert clock.today(CHICAGO_EVENING) == datetime.date(2026, 1, 15)


def test_today_follows_another_configured_zone(monkeypatch: pytest.MonkeyPatch):
    """The date comes from the configured zone, not a fixed one."""
    zone = clock.read_timezone(_config("[server]\ntimezone = Asia/Tokyo\n"))
    monkeypatch.setattr(clock, "TIMEZONE", zone)
    assert clock.today(CHICAGO_EVENING) == datetime.date(2026, 1, 16)


def test_missing_option_falls_back_to_chicago():
    """A config without the option uses the default zone."""
    assert clock.read_timezone(_config("[server]\n")).key == "America/Chicago"


@pytest.mark.parametrize("name", ["Nowhere/Land", "", "../etc/passwd"])
def test_unknown_timezone_fails_clearly(name):
    """An unknown zone name is an error naming the option and the value."""
    config = _config(f"[server]\ntimezone = {name}\n")
    with pytest.raises(ValueError, match=r"\[server\] timezone = .* is not a known"):
        clock.read_timezone(config)


def test_server_config_returns_the_timezone(client):
    """GET /server-config reports the configured zone."""
    response = client.get("/server-config")
    assert response.status_code == 200
    assert response.json() == {"timezone": "America/Chicago"}


def test_server_config_follows_the_config(client, monkeypatch: pytest.MonkeyPatch):
    """The endpoint reports whatever zone the config file set."""
    monkeypatch.setattr(clock, "TIMEZONE", clock.ZoneInfo("Asia/Tokyo"))
    assert client.get("/server-config").json() == {"timezone": "Asia/Tokyo"}


def test_server_config_is_read_only(client):
    """The zone comes from the config file and cannot be changed over the API."""
    response = client.put("/server-config", json={"timezone": "Asia/Tokyo"})
    assert response.status_code == 405
