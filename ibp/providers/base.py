"""IBP inmate search utility."""

import functools
import logging
import typing

from . import fbop, tdcj

Jurisdiction = typing.Literal["Texas", "Federal"]

PROVIDERS: dict[Jurisdiction, typing.Any] = {
    "Texas": tdcj,
    "Federal": fbop,
}

LOGGERS: dict[Jurisdiction, logging.Logger] = {
    "Texas": tdcj.LOGGER,
    "Federal": fbop.LOGGER,
}


def wrap_query(wrapped):
    """query function."""

    @functools.wraps(wrapped)
    async def wrapper(
        *args,
        jurisdictions: typing.Optional[typing.Iterable[Jurisdiction]] = None,
        timeout: typing.Optional[float] = None,
    ):
        if jurisdictions is None:
            jurisdictions = PROVIDERS.keys()

        jurisdictions = list(set(jurisdictions))
        for jurisdiction in jurisdictions:
            if jurisdiction not in PROVIDERS:
                raise ValueError(f"Invalid jurisdiction '{jurisdiction}' given.")

        inmates = []
        errors = []

        for jurisdiction in jurisdictions:
            logger = LOGGERS[jurisdiction]
            try:
                result = await wrapped(*args, jurisdiction, timeout=timeout)
                inmates.extend(result)
            except Exception as error:  # pylint: disable=broad-exception-caught
                error_name = error.__class__.__name__
                message = f"Query returned '{error_name}: {error}'."
                logger.error(message)
                errors.append(error)

        return inmates, errors

    return wrapper


@wrap_query
async def query_by_inmate_id(
    inmate_id: str | int,
    jurisdiction: Jurisdiction,
    timeout: typing.Optional[float] = 10.0,
):
    """Query jurisdiction with an inmate ID."""
    provider = PROVIDERS[jurisdiction]
    logger = LOGGERS[jurisdiction]

    try:
        inmate_id = provider.format_inmate_id(inmate_id)
    except ValueError as exc:
        msg = f"'{inmate_id}' is not a valid Texas inmate number"
        raise ValueError(msg) from exc

    logger.debug("Querying with ID '%s'", inmate_id)
    matches = await provider.query(inmate_id=inmate_id, timeout=timeout)

    if not matches:
        logger.debug("No results returned")

    if len(matches) == 1:
        logger.debug("A single result was returned")

    if len(matches) > 1:
        logger.error("Multiple results were returned for an ID query")

    return matches


@wrap_query
async def query_by_name(
    first: str,
    last: str,
    jurisdiction: Jurisdiction,
    timeout: typing.Optional[float] = 10.0,
):
    """Query jurisdiction with an inmate name."""
    provider = PROVIDERS[jurisdiction]
    logger = LOGGERS[jurisdiction]

    logger.debug("Querying with name '%s, %s'", last, first)
    matches = await provider.query(first_name=first, last_name=last, timeout=timeout)

    if not matches:
        logger.debug("No results were returned")
        return matches

    for inmate in matches:
        logger.debug(
            "%s, %s #%s: MATCHES",
            inmate.last_name,
            inmate.first_name,
            inmate.id,
        )

    logger.debug("%d result(s) returned", len(matches))
    return matches
