"""Normalize the race and sex values providers report to one vocabulary.

TDCJ reports one-letter codes and FBOP reports words; both are stored as the
canonical words below, so a ``QueryResult`` means the same for either
jurisdiction. ``Unknown`` is a value a provider reported; a missing or empty
value is ``None``. A value not in these tables is passed through unchanged,
with a warning, so a new code shows up rather than breaking a search.
"""

import logging
import typing

LOGGER: logging.Logger = logging.getLogger(__name__)

Jurisdiction = typing.Literal["Federal", "Texas"]

RACES = (
    "White",
    "Black",
    "Hispanic",
    "Asian",
    "American Indian",
    "Other",
    "Unknown",
)

SEXES = ("Male", "Female", "Unknown")

# TDCJ publishes no legend for its codes. W, B, H and A are plain; I
# (American Indian), O (Other) and U (Unknown) are assumed meanings.
TDCJ_RACES = {
    "W": "White",
    "B": "Black",
    "H": "Hispanic",
    "A": "Asian",
    "I": "American Indian",
    "O": "Other",
    "U": "Unknown",
}

TDCJ_SEXES = {
    "M": "Male",
    "F": "Female",
    "U": "Unknown",
}

# FBOP's JSON results carry words already (it has no Hispanic category); its
# search form uses these codes, which are mapped in case results ever do.
FBOP_RACES = {
    "W": "White",
    "B": "Black",
    "A": "Asian",
    "I": "American Indian",
}

FBOP_SEXES = {
    "M": "Male",
    "F": "Female",
}

PROVIDER_RACES: dict[Jurisdiction, dict[str, str]] = {
    "Texas": TDCJ_RACES,
    "Federal": FBOP_RACES,
}

PROVIDER_SEXES: dict[Jurisdiction, dict[str, str]] = {
    "Texas": TDCJ_SEXES,
    "Federal": FBOP_SEXES,
}


def _lookup(canonical: tuple[str, ...], codes: dict[str, str]) -> dict[str, str]:
    """Map each provider code and canonical word, lowercased, to its word."""
    table = {word.lower(): word for word in canonical}
    table.update((code.lower(), word) for code, word in codes.items())
    return table


_RACE_LOOKUP = {
    jurisdiction: _lookup(RACES, codes)
    for jurisdiction, codes in PROVIDER_RACES.items()
}

_SEX_LOOKUP = {
    jurisdiction: _lookup(SEXES, codes)
    for jurisdiction, codes in PROVIDER_SEXES.items()
}


def _normalize(
    lookup: dict[str, str],
    jurisdiction: Jurisdiction,
    field: str,
    raw: typing.Optional[str],
) -> typing.Optional[str]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return lookup[" ".join(value.split()).lower()]
    except KeyError:
        LOGGER.warning(
            "Unrecognized %s %s value %r; storing it unchanged",
            jurisdiction,
            field,
            value,
        )
        return value


def normalize_race(
    jurisdiction: Jurisdiction, raw: typing.Optional[str]
) -> typing.Optional[str]:
    """Return the canonical race for a provider's ``raw`` value."""
    return _normalize(_RACE_LOOKUP[jurisdiction], jurisdiction, "race", raw)


def normalize_sex(
    jurisdiction: Jurisdiction, raw: typing.Optional[str]
) -> typing.Optional[str]:
    """Return the canonical sex for a provider's ``raw`` value."""
    return _normalize(_SEX_LOOKUP[jurisdiction], jurisdiction, "sex", raw)
