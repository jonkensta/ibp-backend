"""Inmate name search: parsing a volunteer's query and the SQL behind it.

Used by ``GET /inmates`` (``api.search_inmates``): which rows a name matches,
in what order, and which matches are set aside as stale.
"""

import datetime
from typing import Iterable

import sqlalchemy
from nameparser import HumanName  # type: ignore[import]
from nameparser.config import Constants  # type: ignore[import]
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import models
from .base import config

# Cap on rows returned by a name search, so unioning multiple candidate
# (first, last) assignments cannot produce absurdly large responses.
MAX_SEARCH_RESULTS = 250

# Name parsing rules for search queries. Volunteers type names, not
# salutations, and some first names (King, Prince) are titles to nameparser.
QUERY_NAME_RULES = Constants(titles=[])

# Words that start a surname ("van", "de"), so begin a query written last name
# first.
PARTICLES = QUERY_NAME_RULES.prefixes


def parse_name_query(query: str) -> list[tuple[str, str]]:
    """Parse a free-form name query into candidate (first, last) pairs.

    Volunteers type names however they appear on an envelope: "first last",
    "last first", or "last, first".  Instead of guessing one (first, last)
    assignment, every plausible assignment is returned and the search matches
    the union of them:

    - "maria garcia" / "garcia maria" / "garcia, maria"
        -> [(first, last), (last, first)] for the two parsed words
    - "van horn john": a surname that starts with a particle, written first
      with no comma, is also read as "van horn, john"

    Words nameparser knows as titles ("king", "prince") are read as names,
    and periods ("j. smith", "st. john") are ignored.

    Both components are required (a first initial is enough): the external
    providers cannot search on a lone name, so a single-word query would
    silently search only the local cache.  An empty list is returned when the
    query does not contain two name parts.
    """
    query = query.replace(".", " ")
    readings = [query]
    words = query.split()
    if "," not in query and len(words) > 2 and words[0].lower() in PARTICLES:
        readings.insert(0, f"{' '.join(words[:-1])}, {words[-1]}")

    candidates: list[tuple[str, str]] = []
    for reading in readings:
        name = HumanName(reading, constants=QUERY_NAME_RULES)
        first: str = name.first
        last: str = name.last
        if not (first and last):
            continue
        pairs = [(first, last)]
        if first.lower() != last.lower():
            pairs.append((last, first))
        candidates.extend(pair for pair in pairs if pair not in candidates)

    return candidates


def old_data_clause():
    """SQL clause for inmates never fetched or not fetched in the grace period.

    The grace period is ``[search] stale_after_days``.
    """
    days = config.getint("search", "stale_after_days")
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days
    )
    return sqlalchemy.or_(
        models.Inmate.datetime_fetched.is_(None),
        models.Inmate.datetime_fetched < cutoff,
    )


def prefix_match(column, term: str):
    """SQL clause: ``column`` starts with ``term``, ignoring case."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(escaped + "%", escape="\\")


def spaceless(name: str) -> str:
    """``name`` without whitespace, as last names are compared.

    TDCJ stores a surname written with spaces run together (DE LA CRUZ as
    DELACRUZ), while FBOP may keep the spaces.
    """
    return "".join(name.split())


def spaceless_last_name():
    """SQL: the inmate's last name without spaces (see :func:`spaceless`).

    A function of the column, so an index on it could not be used; the name
    columns have none, and the case-insensitive match scans the table anyway.
    """
    return sqlalchemy.func.replace(models.Inmate.last_name, " ", "")


def is_initial(name: str) -> bool:
    """Whether a name part is only an initial, like "J" or "J."."""
    return len(name.strip(" .")) <= 1


def provider_would_return_clause(jurisdiction: str, candidates: list[tuple[str, str]]):
    """SQL clause for ``jurisdiction``'s rows its provider's search returns.

    The providers match names more narrowly than the local search: TDCJ
    needs the last name exactly, spaces aside (see :func:`spaceless`), and
    the first name as a prefix; FBOP needs both names exactly and finds
    nobody by an initial. Both ignore case. A row matches if the provider
    would have returned it for any of the ``candidates`` (first, last)
    assignments.
    """
    lower = sqlalchemy.func.lower
    first_name, last_name = models.Inmate.first_name, models.Inmate.last_name

    if jurisdiction == "Texas":
        matches = [
            sqlalchemy.and_(
                lower(spaceless_last_name()) == spaceless(last).lower(),
                prefix_match(first_name, first),
            )
            for first, last in candidates
        ]
    elif jurisdiction == "Federal":
        matches = [
            sqlalchemy.and_(
                lower(first_name) == first.lower(), lower(last_name) == last.lower()
            )
            for first, last in candidates
            if not is_initial(first)
        ]
    else:
        matches = []

    return sqlalchemy.and_(
        models.Inmate.jurisdiction == jurisdiction,
        sqlalchemy.or_(sqlalchemy.false(), *matches),
    )


def stale_inmate_clause(
    candidates: list[tuple[str, str]], errors: list, incomplete: Iterable[str] = ()
):
    """SQL clause for the inmates a name search should set aside as stale.

    A name search upserts everything the providers return before it reads the
    table, so a row its provider would have returned (see
    :func:`provider_would_return_clause`) that is still old afterwards is one
    the provider no longer lists, most likely a released inmate. Such a row
    is stale once its data is older than ``[search] stale_after_days`` (or was
    never fetched); the grace period covers a provider that briefly omits
    someone. A local match the provider's own search would not have returned
    says nothing about that person, so it is never stale.

    A provider that failed, or capped its answer (``incomplete`` lists those
    jurisdictions), may have left out people it still lists, so none of that
    jurisdiction's rows are stale: the table then serves as a cache. Each
    error carries its provider's ``jurisdiction`` (see ``providers.base``);
    one that does not could be any provider's, so nothing is stale.
    """
    failed = {getattr(error, "jurisdiction", None) for error in errors}
    if None in failed:
        return sqlalchemy.false()

    unsure = failed | set(incomplete)
    answered = [
        provider_would_return_clause(jurisdiction, candidates)
        for jurisdiction in ("Texas", "Federal")
        if jurisdiction not in unsure
    ]
    return sqlalchemy.and_(
        old_data_clause(), sqlalchemy.or_(sqlalchemy.false(), *answered)
    )


async def query_inmates_by_name(
    session: AsyncSession, candidates: list[tuple[str, str]], where=None
):
    """Query inmates table by name.

    ``candidates`` holds the plausible (first, last) assignments of the
    user's query (see :func:`parse_name_query`).  A row matches if any
    assignment matches, where each non-empty component is a case-insensitive
    prefix match, last names compared without spaces (see :func:`spaceless`),
    and ``where`` (an extra SQL clause, if given) holds.
    Results are ordered with exact last-name matches first, then by name, and
    capped at ``MAX_SEARCH_RESULTS`` rows. Among inmates with the same name,
    recently fetched ones come first (see :func:`old_data_clause`), so old
    records never sit above the people a provider just returned.
    """
    lower = sqlalchemy.func.lower

    matches = []
    for first, last in candidates:
        clauses = []
        if last:
            clauses.append(prefix_match(spaceless_last_name(), spaceless(last)))
        if first:
            clauses.append(prefix_match(models.Inmate.first_name, first))
        if clauses:
            matches.append(sqlalchemy.and_(*clauses))

    tokens = sorted(
        {spaceless(part).lower() for pair in candidates for part in pair if part}
    )
    exact_last_rank = sqlalchemy.case(
        (lower(spaceless_last_name()).in_(tokens), 0), else_=1
    )

    statement = (
        select(models.Inmate)
        .where(sqlalchemy.or_(*matches))
        .where(sqlalchemy.true() if where is None else where)
        .order_by(
            exact_last_rank,
            lower(models.Inmate.last_name),
            lower(models.Inmate.first_name),
            sqlalchemy.case((old_data_clause(), 1), else_=0),
            models.Inmate.jurisdiction,
            models.Inmate.id,
        )
        .limit(MAX_SEARCH_RESULTS)
    )

    return (await session.execute(statement)).scalars().all()
