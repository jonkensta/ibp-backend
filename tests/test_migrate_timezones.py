"""Tests for converting legacy US Central naive datetimes to UTC."""

import datetime

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

import ibp.models

UTC = datetime.timezone.utc


@pytest.mark.parametrize(
    ("legacy", "expected_utc"),
    [
        # CDT (UTC-5): the example from the legacy comments table.
        (
            "2026-10-09 14:00:31.666803",
            datetime.datetime(2026, 10, 9, 19, 0, 31, 666803, tzinfo=UTC),
        ),
        # CST (UTC-6).
        (
            "2026-01-15 09:30:00.000000",
            datetime.datetime(2026, 1, 15, 15, 30, tzinfo=UTC),
        ),
        # CST late in the evening crosses the UTC date boundary.
        (
            "2025-12-31 23:15:00.000000",
            datetime.datetime(2026, 1, 1, 5, 15, tzinfo=UTC),
        ),
    ],
)
def test_parse_datetime_converts_central_to_utc(migrate, legacy, expected_utc):
    """Naive legacy strings are interpreted as America/Chicago."""
    parsed = migrate.parse_datetime(legacy)
    assert parsed == expected_utc
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_dst_spring_forward_boundary(migrate):
    """Times either side of the 2026-03-08 spring-forward use CST then CDT."""
    before = migrate.legacy_local_to_utc(datetime.datetime(2026, 3, 8, 1, 59, 59))
    after = migrate.legacy_local_to_utc(datetime.datetime(2026, 3, 8, 3, 0, 0))
    assert before == datetime.datetime(2026, 3, 8, 7, 59, 59, tzinfo=UTC)
    assert after == datetime.datetime(2026, 3, 8, 8, 0, 0, tzinfo=UTC)


def test_dst_nonexistent_time_uses_pre_transition_offset(migrate):
    """02:30 on spring-forward day does not exist; fold=0 applies CST."""
    converted = migrate.legacy_local_to_utc(datetime.datetime(2026, 3, 8, 2, 30))
    assert converted == datetime.datetime(2026, 3, 8, 8, 30, tzinfo=UTC)


def test_dst_ambiguous_time_resolves_to_first_occurrence(migrate):
    """01:30 on fall-back day occurs twice; fold=0 picks the CDT occurrence."""
    converted = migrate.legacy_local_to_utc(datetime.datetime(2026, 11, 1, 1, 30))
    assert converted == datetime.datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_rejects_aware_input(migrate):
    """Already-aware datetimes indicate a bug and must not be re-shifted."""
    with pytest.raises(ValueError):
        migrate.legacy_local_to_utc(datetime.datetime(2026, 1, 1, tzinfo=UTC))


def test_parse_datetime_or_none(migrate):
    """None passes through untouched."""
    assert migrate.parse_datetime_or_none(None) is None
    assert migrate.parse_datetime_or_none(
        "2026-07-04 12:00:00.000000"
    ) == datetime.datetime(2026, 7, 4, 17, 0, tzinfo=UTC)


def test_converted_datetime_round_trips_through_tzdatetime(migrate):
    """A converted value is stored as UTC and loads back as the same instant."""
    engine = sqlalchemy.create_engine("sqlite://")
    ibp.models.Base.metadata.create_all(engine)
    created = migrate.parse_datetime("2026-10-09 14:00:31.666803")

    with Session(engine) as session:
        session.add(
            ibp.models.Inmate(
                jurisdiction="Texas",
                id=1,
                comments=[
                    ibp.models.Comment(
                        index=0, datetime_created=created, author="a", body="b"
                    )
                ],
            )
        )
        session.commit()

    with engine.connect() as connection:
        raw = connection.execute(
            sqlalchemy.text("SELECT datetime_created FROM comments")
        ).scalar_one()
    # SQLite stores the naive UTC wall-clock time.
    assert raw == "2026-10-09 19:00:31.666803"

    with Session(engine) as session:
        comment = session.scalars(sqlalchemy.select(ibp.models.Comment)).one()
        assert comment.datetime_created == created
        assert comment.datetime_created.tzinfo is not None
        assert comment.datetime_created.utcoffset() == datetime.timedelta(0)
