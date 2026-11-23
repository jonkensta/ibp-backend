"""Provider race and sex values normalized to one vocabulary."""

import logging

import pytest

from ibp.providers import normalize
from ibp.providers.normalize import normalize_race, normalize_sex

TEXAS_RACES = [
    ("W", "White"),
    ("B", "Black"),
    ("H", "Hispanic"),
    ("A", "Asian"),
    ("I", "American Indian"),
    ("O", "Other"),
    ("U", "Unknown"),
]
TEXAS_SEXES = [("M", "Male"), ("F", "Female"), ("U", "Unknown")]
FEDERAL_RACES = [
    ("White", "White"),
    ("Black", "Black"),
    ("American Indian", "American Indian"),
    ("Asian", "Asian"),
    ("W", "White"),
    ("B", "Black"),
    ("I", "American Indian"),
    ("A", "Asian"),
]
FEDERAL_SEXES = [("Male", "Male"), ("Female", "Female"), ("M", "Male"), ("F", "Female")]

FUNCTIONS = {"race": normalize_race, "sex": normalize_sex}
CANONICAL = {"race": normalize.RACES, "sex": normalize.SEXES}


@pytest.fixture(autouse=True)
def enabled_logger(monkeypatch):
    """Re-enable the module's logger.

    Alembic's fileConfig (run by the migration tests) disables loggers that
    already exist, this one among them.
    """
    monkeypatch.setattr(normalize.LOGGER, "disabled", False)


@pytest.mark.parametrize(
    "jurisdiction, field, raw, expected",
    [("Texas", "race", raw, word) for raw, word in TEXAS_RACES]
    + [("Texas", "sex", raw, word) for raw, word in TEXAS_SEXES]
    + [("Federal", "race", raw, word) for raw, word in FEDERAL_RACES]
    + [("Federal", "sex", raw, word) for raw, word in FEDERAL_SEXES],
)
def test_known_values_are_canonical(jurisdiction, field, raw, expected, caplog):
    """Each provider value, in any case, maps to its word with no warning."""
    function = FUNCTIONS[field]
    with caplog.at_level(logging.WARNING, logger=normalize.__name__):
        assert function(jurisdiction, raw) == expected
        assert function(jurisdiction, raw.lower()) == expected
        assert function(jurisdiction, f" {raw.upper()} ") == expected
    assert not caplog.records


def test_tables_cover_every_texas_code():
    """The tested Texas codes are exactly the module's tables."""
    assert dict(TEXAS_RACES) == normalize.TDCJ_RACES
    assert dict(TEXAS_SEXES) == normalize.TDCJ_SEXES


@pytest.mark.parametrize("jurisdiction", ["Texas", "Federal"])
@pytest.mark.parametrize("field", ["race", "sex"])
def test_canonical_words_are_unchanged(jurisdiction, field, caplog):
    """Normalizing a canonical word gives it back, so normalizing is idempotent."""
    function = FUNCTIONS[field]
    with caplog.at_level(logging.WARNING, logger=normalize.__name__):
        for word in CANONICAL[field]:
            assert function(jurisdiction, word) == word
            assert function(jurisdiction, function(jurisdiction, word)) == word
    assert not caplog.records


@pytest.mark.parametrize("jurisdiction", ["Texas", "Federal"])
@pytest.mark.parametrize("field", ["race", "sex"])
@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_missing_values_are_none(jurisdiction, field, raw, caplog):
    """A missing, empty or blank value is no data, not Unknown."""
    with caplog.at_level(logging.WARNING, logger=normalize.__name__):
        assert FUNCTIONS[field](jurisdiction, raw) is None
    assert not caplog.records


def test_inner_whitespace_is_ignored():
    """Extra spaces inside a word still match it."""
    assert normalize_race("Federal", "American   Indian") == "American Indian"


@pytest.mark.parametrize(
    "jurisdiction, field, raw",
    [
        ("Texas", "race", "X"),
        ("Texas", "sex", "Q"),
        ("Federal", "race", "Pacific Islander"),
        ("Federal", "sex", "X"),
        # FBOP has no Hispanic code, so H is not guessed at.
        ("Federal", "race", "H"),
    ],
)
def test_unseen_value_passes_through_with_warning(jurisdiction, field, raw, caplog):
    """A value never seen before is kept, stripped, and a warning names it."""
    with caplog.at_level(logging.WARNING, logger=normalize.__name__):
        assert FUNCTIONS[field](jurisdiction, f" {raw} ") == raw
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert jurisdiction in message
    assert field in message
    assert repr(raw) in message
