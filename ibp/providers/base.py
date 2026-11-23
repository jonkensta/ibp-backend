"""IBP inmate search utility."""

import functools
import logging
import typing

from . import fbop, tdcj
from .types import Answer, QueryResult

Jurisdiction = typing.Literal["Texas", "Federal"]

PROVIDERS: dict[Jurisdiction, typing.Any] = {
    "Texas": tdcj,
    "Federal": fbop,
}

LOGGERS: dict[Jurisdiction, logging.Logger] = {
    "Texas": tdcj.LOGGER,
    "Federal": fbop.LOGGER,
}


class Results(typing.NamedTuple):
    """Every asked provider's answers to one query.

    ``errors`` holds the providers that failed, each error tagged with its
    ``jurisdiction``. ``incomplete`` lists the jurisdictions whose provider
    answered but capped its answer (see :class:`Answer`); those are not
    errors.
    """

    inmates: list[QueryResult]
    errors: list[Exception]
    incomplete: list[Jurisdiction]


def wrap_query(wrapped):
    """Ask each jurisdiction's provider in turn and gather their answers.

    ``wrapped`` returns one provider's :class:`Answer`; the wrapper returns
    :class:`Results`.
    """

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

        inmates: list[QueryResult] = []
        errors: list[Exception] = []
        incomplete: list[Jurisdiction] = []

        for jurisdiction in jurisdictions:
            logger = LOGGERS[jurisdiction]
            try:
                answer: Answer = await wrapped(*args, jurisdiction, timeout=timeout)
            except Exception as error:  # pylint: disable=broad-exception-caught
                error_name = error.__class__.__name__
                message = f"Query returned '{error_name}: {error}'."
                logger.error(message)
                # Lets a caller tell which provider failed (see api.search_inmates).
                error.jurisdiction = jurisdiction  # type: ignore[attr-defined]
                errors.append(error)
                continue

            inmates.extend(answer.inmates)
            if not answer.complete:
                logger.warning("Query answer was capped; it may not list everyone")
                incomplete.append(jurisdiction)

        return Results(inmates, errors, incomplete)

    return wrapper


@wrap_query
async def query_by_inmate_id(
    inmate_id: str | int,
    jurisdiction: Jurisdiction,
    timeout: typing.Optional[float] = 10.0,
) -> Answer:
    """Query jurisdiction with an inmate ID."""
    provider = PROVIDERS[jurisdiction]
    logger = LOGGERS[jurisdiction]

    try:
        inmate_id = provider.format_inmate_id(inmate_id)
    except ValueError as exc:
        msg = f"not a valid {jurisdiction} inmate number"
        raise ValueError(msg) from exc

    logger.debug("Querying by inmate ID")
    answer = await provider.query(inmate_id=inmate_id, timeout=timeout)
    matches = answer.inmates

    if not matches:
        logger.debug("No results returned")

    if len(matches) == 1:
        logger.debug("A single result was returned")

    if len(matches) > 1:
        logger.error("Multiple results were returned for an ID query")

    return answer


@wrap_query
async def query_by_name(
    first: str,
    last: str,
    jurisdiction: Jurisdiction,
    timeout: typing.Optional[float] = 10.0,
) -> Answer:
    """Query jurisdiction with an inmate name."""
    provider = PROVIDERS[jurisdiction]
    logger = LOGGERS[jurisdiction]

    logger.debug("Querying by name")
    answer = await provider.query(first_name=first, last_name=last, timeout=timeout)

    if not answer.inmates:
        logger.debug("No results were returned")
        return answer

    logger.debug("%d result(s) returned", len(answer.inmates))
    return answer
