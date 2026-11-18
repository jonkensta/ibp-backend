"""The UPC -> ISBN map volunteers fill: validation, endpoints, migration."""

# pylint: disable=redefined-outer-name

import asyncio
import datetime

import pytest
import sqlalchemy
from alembic import command

from ibp import books, db_bootstrap, models, upcs

HOBBIT = "9780547928227"
CRIME = "9780140449136"


def with_check_digit(first11: str) -> str:
    """A UPC-A: 11 digits plus their check digit."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(first11))
    return first11 + str((10 - total % 10) % 10)


UPC = with_check_digit("07099900123")
UPC_ADD_ON = UPC + "00799"
# Another title sharing the same 12-digit UPC (publisher + price).
UPC_OTHER_ADD_ON = UPC + "00899"
BAD_CHECK = UPC[:11] + str((int(UPC[11]) + 1) % 10)


def test_upc_check_digit():
    """The weights are 3,1,3,1... from the left."""
    assert upcs.upc_check_digit_ok("012345678905")
    assert not upcs.upc_check_digit_ok("012345678904")


@pytest.mark.parametrize(
    ("code", "valid"),
    [
        (UPC, True),
        (UPC + "12", True),  # 2-digit add-on
        (UPC_ADD_ON, True),  # 5-digit add-on
        (BAD_CHECK, False),  # bad check digit
        (UPC + "1", False),  # 13 digits: not a UPC length
        (UPC + "123", False),
        (UPC[:11], False),
        ("0" + UPC, False),  # leading EAN 0 is stripped by the extension
        (UPC[:6] + "a" + UPC[7:], False),
        ("", False),
    ],
)
def test_valid_code(code, valid):
    """Digits only, 12/14/17 long, UPC-A check digit on the first 12."""
    assert upcs.valid_code(code) is valid


# --- Endpoints ---------------------------------------------------------------


def stub_books(monkeypatch, known=(CRIME, HOBBIT)):
    """Make books.lookup know ``known``; return the ISBNs looked up."""
    calls = []

    async def lookup(isbn):
        calls.append(isbn)
        if isbn not in known:
            return None
        return books.BookResult(
            title=f"Title {isbn}",
            author="Some Author",
            source="claude",
            confidence="high",
            candidates="{}",
            cacheable=True,
        )

    monkeypatch.setattr(books, "lookup", lookup)
    return calls


def add_row(api_db, code, isbn, source="volunteer"):
    """Seed a ``upcs`` row."""
    with api_db() as session:
        session.add(
            models.Upc(
                upc=code,
                isbn=isbn,
                source=source,
                looked_up_at=datetime.datetime.now(datetime.timezone.utc),
            )
        )
        session.commit()


def get_row(api_db, code):
    """The ``upcs`` row for ``code`` as (isbn, source), or None."""
    with api_db() as session:
        row = session.get(models.Upc, code)
        return None if row is None else (row.isbn, row.source)


@pytest.mark.parametrize("code", [BAD_CHECK, "0" + UPC, UPC + "1", "abc"])
def test_get_rejects_invalid_code(client, code):
    """A bad code is a 422."""
    assert client.get(f"/books/upc/{code}").status_code == 422


@pytest.mark.parametrize("code", [UPC, UPC_ADD_ON])
def test_get_unknown_is_404(client, api_db, code):
    """A code with no row is a no_isbn 404, and nothing is stored."""
    response = client.get(f"/books/upc/{code}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "No ISBN known for this UPC.",
        "reason": "no_isbn",
    }
    assert get_row(api_db, code) is None


@pytest.mark.parametrize("source", ["upcitemdb", "barcodelookup"])
def test_get_leftover_service_miss_is_404(client, api_db, source):
    """A NULL-ISBN row from the removed lookup services answers nothing."""
    add_row(api_db, UPC, None, source=source)
    assert client.get(f"/books/upc/{UPC}").json()["reason"] == "no_isbn"
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["reason"] == "no_isbn"


def test_get_answers_from_map(client, api_db, monkeypatch):
    """A mapped code answers with the ISBN's book, cached after the first time."""
    add_row(api_db, UPC_ADD_ON, CRIME)
    book_calls = stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert response.json() == {
        "isbn": CRIME,
        "title": f"Title {CRIME}",
        "author": "Some Author",
        "source": "claude",
        "confidence": "high",
        "upc_source": "volunteer",
    }
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["isbn"] == CRIME
    assert book_calls == [CRIME]


def test_get_needs_exact_code(client, api_db, monkeypatch):
    """A full code never falls back to its 12 digits, nor to another add-on."""
    add_row(api_db, UPC, HOBBIT, source="upcitemdb")
    add_row(api_db, UPC_ADD_ON, CRIME)
    stub_books(monkeypatch)
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["isbn"] == CRIME
    assert client.get(f"/books/upc/{UPC}").json()["isbn"] == HOBBIT
    assert client.get(f"/books/upc/{UPC_OTHER_ADD_ON}").status_code == 404
    assert client.get(f"/books/upc/{UPC}12").status_code == 404


def test_get_isbn_not_found_is_404(client, api_db, monkeypatch):
    """The code maps to an ISBN no provider knows: 404, mapping kept."""
    add_row(api_db, UPC_ADD_ON, CRIME)
    stub_books(monkeypatch, known=())
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Book not found.",
        "reason": "no_book",
        "isbn": CRIME,
    }
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "volunteer")


def test_put_requires_add_on(client, api_db):
    """A bare 12-digit UPC can't be mapped: 422."""
    response = client.put(f"/books/upc/{UPC}", json={"isbn": CRIME})
    assert response.status_code == 422
    assert get_row(api_db, UPC) is None


def test_put_rejects_invalid_code_and_isbn(client, api_db):
    """A bad code or an ISBN that doesn't normalize: 422."""
    assert client.put("/books/upc/123", json={"isbn": CRIME}).status_code == 422
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": "9780140449137"})
    assert response.status_code == 422
    assert client.put(f"/books/upc/{UPC_ADD_ON}", json={}).status_code == 422
    assert get_row(api_db, UPC_ADD_ON) is None


def test_put_records_mapping_then_get_uses_it(client, api_db, monkeypatch):
    """A volunteer's ISBN (normalized) is stored; GET then returns it."""
    assert client.get(f"/books/upc/{UPC_ADD_ON}").status_code == 404
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": "0140449132"})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "volunteer")

    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        CRIME,
        "volunteer",
    )
    # Another title sharing the 12 digits is still unknown.
    assert client.get(f"/books/upc/{UPC_OTHER_ADD_ON}").status_code == 404


def test_put_beside_leftover_service_miss(client, api_db):
    """A 12-digit miss from the removed services doesn't block its add-ons."""
    add_row(api_db, UPC, None, source="upcitemdb")
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (HOBBIT, "volunteer")
    assert get_row(api_db, UPC) == (None, "upcitemdb")


def test_put_conflict_keeps_mapping(client, api_db):
    """A code mapped to another ISBN is a 409 and keeps its mapping."""
    add_row(api_db, UPC_ADD_ON, CRIME)
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 409
    assert response.json()["detail"] == f"This UPC is already mapped to ISBN {CRIME}."
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "volunteer")


def test_put_same_isbn_is_noop(client, api_db):
    """Putting the ISBN already mapped is a 204 that keeps the row."""
    add_row(api_db, UPC_ADD_ON, CRIME, source="upcitemdb")
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": CRIME})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "upcitemdb")


def test_write_upc_keeps_first_row(api_db):
    """write_upc inserts once; a later write returns the row already there."""
    # pylint: disable=import-outside-toplevel
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from ibp import book_api

    with api_db() as session:
        path = session.get_bind().url.database

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine) as session:
                inserted = await book_api.write_upc(session, UPC_ADD_ON, CRIME)
                kept = await book_api.write_upc(session, UPC_ADD_ON, HOBBIT)
            return inserted, kept
        finally:
            await engine.dispose()

    inserted, kept = asyncio.run(run())
    assert (inserted.isbn, inserted.source) == (CRIME, "volunteer")
    assert (kept.isbn, kept.source) == (CRIME, "volunteer")
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "volunteer")


# --- Migration ---------------------------------------------------------------


def table_names(url):
    """Tables in the database at ``url``."""
    engine = sqlalchemy.create_engine(url)
    try:
        return set(sqlalchemy.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upcs_migration_up_and_down(tmp_path):
    """The upcs migration creates the table and its downgrade drops it."""
    url = f"sqlite:///{tmp_path / 'upcs.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_UPCS_REVISION)
    assert "upcs" not in table_names(url)
    command.upgrade(config, "head")
    assert "upcs" in table_names(url)
    command.downgrade(config, db_bootstrap.PRE_UPCS_REVISION)
    assert "upcs" not in table_names(url)


def test_bootstrap_adopts_pre_upcs_create_all_schema(tmp_path, monkeypatch):
    """A create_all schema from before upcs gets the upcs migration."""
    url = f"sqlite:///{tmp_path / 'preupcs.db'}"
    engine = sqlalchemy.create_engine(url)
    tables = [t for name, t in models.Base.metadata.tables.items() if name != "upcs"]
    models.Base.metadata.create_all(engine, tables=tables)
    engine.dispose()

    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()
    assert "upcs" in table_names(url)
