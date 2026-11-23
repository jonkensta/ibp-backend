"""The server's time zone (``[server] timezone``) and today's date in it."""

import configparser
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import config

DEFAULT_TIMEZONE = "America/Chicago"


def read_timezone(config_: configparser.ConfigParser) -> ZoneInfo:
    """Return the configured time zone, failing clearly on an unknown name."""
    name = config_.get("server", "timezone", fallback=DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"[server] timezone = {name!r} is not a known IANA time zone name "
            f"(e.g. {DEFAULT_TIMEZONE})"
        ) from exc


#: Read once at import, so a bad name stops the server at startup.
TIMEZONE = read_timezone(config)


def today(now: datetime.datetime | None = None) -> datetime.date:
    """Return today's date in the configured time zone.

    ``now`` (an aware datetime, the current time by default) is for tests.
    The process's own local time zone (``TZ``) plays no part.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(TIMEZONE).date()
