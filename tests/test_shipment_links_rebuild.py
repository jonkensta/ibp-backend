"""How the link rebuild reads and writes: reads first, one write at the end."""

# pylint: disable=redefined-outer-name

import bisect
import datetime
import random

import pytest
import sqlalchemy as sa

from ibp import models, shipment_links, shipment_sync
from ibp.shipment_links import (
    BULK_LEAD,
    BULK_LINK_WINDOW,
    LinkKey,
    _bulk_sessions,
    _inmate_number_links,
    _reference_links,
    compute_links,
    never_mailed,
    rebuild_links,
    request_unit,
    write_links,
)
from ibp.unit_places import Places

from .shipment_helpers import add_inmate, add_request, add_shipment, add_unit, links

NOW = datetime.datetime(2026, 4, 20, 12, tzinfo=datetime.timezone.utc)
DELIVERED = (("2026-01-01T00:00", "delivered"),)


def per_place_links(session, now) -> dict[LinkKey, str]:
    """The links as the rebuild worked them out before it read every
    place's requests in one query: one query per place. The oracle the
    one-query rebuild must agree with."""
    shipments = [
        shipment
        for shipment in session.scalars(
            sa.select(models.Shipment).order_by(models.Shipment.id)
        ).all()
        if not never_mailed(shipment)
    ]
    found: dict[LinkKey, str] = {}
    _reference_links(session, shipments, found)
    _inmate_number_links(session, shipments, found)
    labelled = {key[:3] for key in found}
    places = Places(session.scalars(sa.select(models.Unit)).all())
    for place, by_date in sorted(_bulk_sessions(shipments, places, now).items()):
        dates = sorted(by_date)
        units = places.members(place)
        rows = session.execute(
            sa.select(
                models.Request.inmate_jurisdiction,
                models.Request.inmate_id,
                models.Request.index,
                models.Request.date_processed,
            )
            .join(
                models.Inmate,
                sa.and_(
                    models.Inmate.jurisdiction == models.Request.inmate_jurisdiction,
                    models.Inmate.id == models.Request.inmate_id,
                ),
            )
            .where(
                models.Inmate.jurisdiction == place[0],
                request_unit().in_(sorted(unit[1] for unit in units)),
                models.Request.action == "Filled",
                models.Request.date_processed >= dates[0] - BULK_LINK_WINDOW,
                models.Request.date_processed <= dates[-1] - BULK_LEAD,
            )
        ).all()
        for jurisdiction, inmate_id, index, processed in rows:
            if (jurisdiction, inmate_id, index) in labelled:
                continue
            position = bisect.bisect_left(dates, processed + BULK_LEAD)
            if position == len(dates) or dates[position] - processed > BULK_LINK_WINDOW:
                continue
            for shipment_id in by_date[dates[position]]:
                found[(jurisdiction, inmate_id, index, shipment_id)] = "unit_session"
    return found


def seed_mixed(session, seed: int) -> None:
    """Invented units (one place under two codes), inmates, requests of
    every kind and labels of every kind, at random."""
    rng = random.Random(seed)
    add_unit(
        session, "MAIN", street1="Main Unit", street2="9 Prison Rd", zipcode="77000"
    )
    add_unit(
        session, "CAMP", street1="Main Unit", street2="9 Prison Road", zipcode="77000"
    )
    for n in range(6):
        add_unit(session, f"U{n}", street1=f"{n + 10} Example Rd", zipcode=f"7710{n}")
    units = ["MAIN", "CAMP", *(f"U{n}" for n in range(6)), None]
    inmates = list(range(20000000, 20000060))
    for inmate_id in inmates:
        add_inmate(session, inmate_id, rng.choice(units))
    start = datetime.date(2026, 1, 1)
    for inmate_id in inmates:
        for index in range(rng.randrange(5)):
            request = add_request(
                session,
                inmate_id,
                index,
                (start + datetime.timedelta(days=rng.randrange(100))).isoformat(),
                action=rng.choice(["Filled", "Filled", "Filled", "Tossed"]),
            )
            if rng.random() < 0.4:
                request.unit_name = rng.choice(units + ["GONE"])
                request.unit_recorded = True
    for _ in range(80):
        bought = datetime.datetime.combine(
            start + datetime.timedelta(days=rng.randrange(110)),
            datetime.time(rng.randrange(24)),
        ).strftime("%Y-%m-%dT%H:%M")
        kind = rng.choice(["individual", "bulk", "bulk", "other"])
        columns: dict = {
            "events": rng.choice([DELIVERED, (), (("2026-01-01T00:00", "unknown"),)]),
            "refund_status": rng.choice([None, None, "refunded"]),
        }
        if kind == "bulk":
            columns.update(unit_jurisdiction="Texas", unit_name=rng.choice(units))
        if kind == "individual":
            columns["inmate_number"] = f"{rng.choice(inmates):08d}"
            if rng.random() < 0.3:
                inmate_id = rng.choice(inmates)
                columns.update(
                    request_jurisdiction="Texas",
                    request_inmate_id=inmate_id,
                    request_index=rng.randrange(3),
                )
        add_shipment(session, bought, kind=kind, **columns)


@pytest.mark.parametrize("seed", range(5))
def test_one_query_rebuild_equals_the_per_place_rebuild(api_db, seed):
    """Reading every place's requests in one query gives exactly the links
    one query per place did."""
    with api_db() as session:
        seed_mixed(session, seed)
        session.commit()
        expected = per_place_links(session, NOW)
        assert any(basis == "unit_session" for basis in expected.values())
        assert compute_links(session, NOW) == expected
        rebuild_links(session, NOW)
        session.commit()
        assert links(session) == {
            (j, i, x, session.get(models.Shipment, s).easypost_id, basis)
            for (j, i, x, s), basis in expected.items()
        }


def statements_of(api_db, action) -> list[str]:
    """The SQL ``action(session)`` runs, in order."""
    seen: list[str] = []
    engine = api_db.kw["bind"]

    def record(conn, cursor, statement, *args):  # pylint: disable=unused-argument
        seen.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        with api_db() as session:
            action(session)
            session.commit()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return seen


def written_table(sql: str):
    """The table a statement writes to, or None for a read."""
    words = sql.split()
    if words[0] in {"INSERT", "DELETE"}:
        return words[2]
    if words[0] == "UPDATE":
        return words[1]
    return None


def test_rebuild_reads_everything_before_its_first_write(api_db):
    """Nothing is written to the database until every read is done, and
    all candidate requests are read in one query whatever the places."""
    with api_db() as session:
        seed_mixed(session, 1)
        session.commit()
    seen = statements_of(api_db, lambda session: rebuild_links(session, NOW))
    main_writes = [
        n
        for n, sql in enumerate(seen)
        if written_table(sql) not in {None, "staged_package_shipments"}
    ]
    assert [seen[n].split(" (")[0] for n in main_writes] == [
        "DELETE FROM package_shipments",
        "INSERT INTO package_shipments",
    ]
    first_write = main_writes[0]
    assert not [
        sql for sql in seen[first_write:] if sql.startswith("SELECT")
    ], "a read after the write lock is taken"
    request_reads = [sql for sql in seen if "FROM requests JOIN inmates" in sql]
    assert len(request_reads) == 1


def test_failed_write_keeps_the_old_links(api_db, monkeypatch):
    """A failure after the old links were deleted rolls the deletion back."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_inmate(session, 11111111, "ALPHA")
        add_request(session, 11111111, 0, "2026-03-01")
        add_shipment(
            session,
            "2026-03-05T15:00",
            kind="bulk",
            unit_jurisdiction="Texas",
            unit_name="ALPHA",
            events=DELIVERED,
        )
        session.commit()
    assert shipment_sync.relink(api_db, NOW) == 1

    def fail(_session):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(shipment_links, "_copy_staged", fail)
    with pytest.raises(RuntimeError):
        shipment_sync.relink(api_db, NOW)
    with api_db() as session:
        assert session.query(models.PackageShipment).count() == 1


def test_request_deleted_after_reading_is_left_out(api_db):
    """A request deleted between the reads and the write loses its link
    instead of breaking the insert."""
    with api_db() as session:
        add_unit(session, "ALPHA")
        add_inmate(session, 11111111, "ALPHA")
        add_inmate(session, 22222222, "ALPHA")
        add_request(session, 11111111, 0, "2026-03-01")
        add_request(session, 22222222, 0, "2026-03-01")
        add_shipment(
            session,
            "2026-03-05T15:00",
            kind="bulk",
            unit_jurisdiction="Texas",
            unit_name="ALPHA",
            events=DELIVERED,
        )
        session.commit()
    with api_db() as session:
        computed = compute_links(session, NOW)
        assert len(computed) == 2
        with api_db() as other:
            other.delete(other.get(models.Request, ("Texas", 22222222, 0)))
            other.commit()
        assert write_links(session, computed) == 1
        session.commit()
        assert {key[1] for key in links(session)} == {11111111}


def seed_individual_and_box(session) -> None:
    """Inmate 11111111 at ALPHA with no request yet, an untagged individual
    label to him and a box to ALPHA, both after the request will be."""
    add_unit(session, "ALPHA")
    add_inmate(session, 11111111, "ALPHA")
    add_shipment(
        session,
        "2026-03-05T15:00",
        kind="individual",
        inmate_number="11111111",
        events=DELIVERED,
    )
    add_shipment(
        session,
        "2026-03-05T16:00",
        kind="bulk",
        unit_jurisdiction="Texas",
        unit_name="ALPHA",
        events=DELIVERED,
    )
    session.commit()


def commit_elsewhere(api_db, change) -> None:
    """Commit ``change(session)`` on another connection."""
    with api_db() as other:
        change(other)
        other.commit()


def test_rebuild_recomputes_when_another_commit_lands_mid_read(api_db, monkeypatch):
    """A Filled request committed between the individual-label lookup and the
    bulk query would get a bulk link from a mixed state; the attempt is
    thrown away and the next one published, equal to a clean recompute."""
    with api_db() as session:
        seed_individual_and_box(session)
    real = shipment_links._unit_session_links
    calls = []

    def interleaved(*args):
        if not calls:
            commit_elsewhere(
                api_db, lambda other: add_request(other, 11111111, 0, "2026-03-01")
            )
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(shipment_links, "_unit_session_links", interleaved)
    assert shipment_sync.relink(api_db, NOW) == 1
    assert len(calls) == 2
    with api_db() as session:
        published = {
            (link.inmate_id, link.shipment_id, link.basis)
            for link in session.query(models.PackageShipment)
        }
    monkeypatch.setattr(shipment_links, "_unit_session_links", real)
    with api_db() as session:
        clean = compute_links(session, NOW)
    assert published == {(key[1], key[3], basis) for key, basis in clean.items()}
    assert {basis for _, _, basis in published} == {"inmate_number"}


def test_rebuild_gives_up_when_every_attempt_is_invalidated(
    api_db, monkeypatch, caplog
):
    """When another connection commits during every attempt, nothing is
    published, the old links stay, and the run fails with one safe line."""
    with api_db() as session:
        seed_individual_and_box(session)
        add_request(session, 11111111, 0, "2026-03-01")
        session.commit()
    assert shipment_sync.relink(api_db, NOW) == 1
    real = shipment_links.compute_links
    attempts = []

    def always_interleaved(session, now=None):
        attempts.append(1)
        found = real(session, now)
        commit_elsewhere(
            api_db,
            lambda other: setattr(
                other.get(models.Unit, ("Texas", "ALPHA")), "url", f"x{len(attempts)}"
            ),
        )
        return found

    monkeypatch.setattr(shipment_links, "compute_links", always_interleaved)
    monkeypatch.setattr(shipment_sync.logger, "disabled", False)
    with caplog.at_level("INFO"):
        assert shipment_sync.main(["--rebuild-links"], sessions=api_db) == 1
    assert len(attempts) == shipment_links.REBUILD_ATTEMPTS
    assert (
        "links not rebuilt: the data changed during each of 5 attempts; "
        "the old links are kept"
    ) in caplog.text
    with api_db() as session:
        assert [link.basis for link in session.query(models.PackageShipment)] == [
            "inmate_number"
        ]


def test_staging_does_not_change_data_version_but_other_commits_do(api_db):
    """data_version is per connection: the rebuild's own staging and delete
    leave it alone, another connection's commit changes it."""
    with api_db() as session:
        seed_individual_and_box(session)
    with api_db() as session:
        before = shipment_links.data_version(session)
        shipment_links.stage_links(session, {("Texas", 11111111, 0, 1): "reference"})
        session.execute(sa.delete(models.PackageShipment))
        assert shipment_links.data_version(session) == before
        session.rollback()
        before = shipment_links.data_version(session)
        commit_elsewhere(
            api_db, lambda other: add_request(other, 11111111, 5, "2026-03-01")
        )
        assert shipment_links.data_version(session) != before


def test_clean_rebuild_is_one_attempt(api_db, monkeypatch):
    """With no other writer, the first attempt is published."""
    with api_db() as session:
        seed_mixed(session, 2)
        session.commit()
    real = shipment_links.compute_links
    attempts = []

    def counted(session, now=None):
        attempts.append(1)
        return real(session, now)

    monkeypatch.setattr(shipment_links, "compute_links", counted)
    shipment_sync.relink(api_db, NOW)
    assert len(attempts) == 1


def test_copy_casts_the_jurisdiction_for_postgresql():
    """On PostgreSQL the staged VARCHAR jurisdiction is cast to the
    requests' native enum, in the join and in the insert."""
    from sqlalchemy.dialects import (
        postgresql,
    )  # pylint: disable=import-outside-toplevel

    sql = str(
        shipment_links.copy_staged_statement().compile(dialect=postgresql.dialect())
    )
    cast_sql = "CAST(staged_package_shipments.inmate_jurisdiction AS jurisdiction_enum)"
    assert sql.count(cast_sql) == 2
    assert f"requests.inmate_jurisdiction = {cast_sql}" in sql


def test_other_databases_rebuild_in_one_repeatable_read_transaction(
    api_db, monkeypatch
):
    """Without SQLite's data_version, the reads and the write share one
    REPEATABLE READ transaction (and are tried once)."""
    with api_db() as session:
        seed_individual_and_box(session)
    with api_db() as session:
        monkeypatch.setattr(session.get_bind().dialect, "name", "postgresql")
        asked = []
        real = session.connection

        def connection(**options):
            asked.append(options)
            return real()

        monkeypatch.setattr(session, "connection", connection)
        monkeypatch.setattr(
            shipment_links,
            "data_version",
            lambda _session: pytest.fail("data_version is SQLite's"),
        )
        rebuild_links(session, NOW)
    assert asked[0] == {"execution_options": {"isolation_level": "REPEATABLE READ"}}
