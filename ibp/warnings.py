"""Methods for generating warnings for inmates and requests."""

import datetime
import itertools
import logging
import typing

from . import models
from .base import config


def _inmate_entry_age_warning(
    inmate_: models.Inmate,
) -> typing.Iterable[typing.Tuple[str, str]]:
    """Yield an entry age warning for an inmate if any."""
    jurisdiction = inmate_.jurisdiction
    id_ = inmate_.id

    key = "entry age"
    try:
        age = datetime.datetime.now() - inmate_.datetime_fetched
    except TypeError:
        msg = f"Data entry for {jurisdiction} inmate #{id_:08d} has never been verified"
        yield key, msg
    else:
        inmates_cache_ttl = config.getint("warnings", "inmates_cache_ttl")
        ttl = datetime.timedelta(hours=inmates_cache_ttl)
        if age > ttl:
            msg = f"Data entry for {jurisdiction} inmate #{id_:08d} is {age.days} day(s) old"
            yield key, msg


def _inmate_release_warning(
    inmate_: models.Inmate,
) -> typing.Iterable[typing.Tuple[str, str]]:
    """Yield a release warning for an inmate if any."""
    try:
        to_release = inmate_.release - datetime.date.today()
    except TypeError:
        return

    key = "release"

    days = config.getint("warnings", "min_release_timedelta")
    min_timedelta = datetime.timedelta(days=days)

    jurisdiction = inmate_.jurisdiction
    id_ = inmate_.id

    if to_release <= datetime.timedelta(0):
        msg = f"{jurisdiction} inmate #{id_:08d} is marked as released"
        yield key, msg

    elif to_release <= min_timedelta:
        msg = (
            f"{jurisdiction} inmate #{id_:08d} is {to_release.days} days from release."
        )
        yield key, msg


def inmate(inmate_: models.Inmate) -> dict[str, str]:
    """Generate warnings about a given inmate."""
    return dict(
        itertools.chain(
            _inmate_entry_age_warning(inmate_), _inmate_release_warning(inmate_)
        )
    )


def _request_postmarkdate_warning(
    inmate_: models.Inmate, postmarkdate: datetime.date
) -> typing.Iterable[typing.Tuple[str, str]]:
    "Yield a postmarkdate warning about a request if any." ""

    def was_filled(request_):
        return request_.action == "Filled"

    requests = filter(was_filled, inmate_.requests)

    try:
        last_filled_request = max(requests, key=lambda request: request.date_postmarked)
    except ValueError:
        return

    td = postmarkdate - last_filled_request.date_postmarked
    days = config.getint("warnings", "min_postmark_timedelta")
    min_td = datetime.timedelta(days=days)

    key = "postmarkdate"
    if td.days < 0:
        yield key, "There is a request with a postmark after this one."
    elif td.days == 0:
        yield key, "No time has transpired since the last postmark."
    elif td < min_td:
        yield key, f"Only {td.days} days since last postmark."


def request(inmate_: models.Inmate, postmarkdate: datetime.date) -> dict[str, str]:
    """Generate warnings about a given request."""
    return dict(itertools.chain(_request_postmarkdate_warning(inmate_, postmarkdate)))
