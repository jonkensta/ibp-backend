"""ISBN lookup: normalization, provider parsing, Claude merge fallback, cache."""

# pylint: disable=redefined-outer-name

import asyncio
import json

import httpx
import pytest
import sqlalchemy
from alembic import command

from ibp import books, db_bootstrap, models

HOBBIT = "9780547928227"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (HOBBIT, HOBBIT),
        ("978-0-547-92822-7", HOBBIT),
        ("0547928211", "9780547928210"),  # ISBN-10 converted
        ("014044913X".replace("X", "2"), "9780140449136"),
        ("080442957X", "9780804429573"),  # X check digit
        (HOBBIT + "51599", HOBBIT),  # 18-digit EAN with price add-on
        ("9780547928228", None),  # bad check digit
        ("0547928212", None),
        ("1234", None),
        ("TEX-1234-0", None),
    ],
)
def test_normalize_isbn(raw, expected):
    """ISBN-10, ISBN-13 and EAN-18 normalize to ISBN-13; junk is rejected."""
    assert books.normalize_isbn(raw) == expected


def test_isbn13_to_isbn10():
    """978 ISBNs have an ISBN-10; 979 ones don't."""
    assert books.isbn13_to_isbn10("9780140449136") == "0140449132"
    assert books.isbn13_to_isbn10("9791032305690") is None


LOC_XML = """<?xml version="1.0"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
<zs:records><zs:record><zs:recordData>
<mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo><nonSort>The </nonSort><title>hobbit</title>
    <subTitle>or, There and back again /</subTitle></titleInfo>
  <titleInfo type="uniform"><title>Ignored uniform title</title></titleInfo>
  <name type="personal" usage="primary">
    <namePart>Tolkien, J. R. R.</namePart><namePart type="date">1892-1973</namePart>
  </name>
  <name type="personal"><namePart>Illustrator, Some,</namePart></name>
  <originInfo><agent><namePart>Houghton Mifflin</namePart></agent>
    <dateIssued>2012</dateIssued></originInfo>
</mods>
</zs:recordData></zs:record></zs:records></zs:searchRetrieveResponse>"""


def test_parse_loc_mods():
    """The main title, its subtitle and the primary name are used."""
    candidate = books.parse_loc_mods(LOC_XML)
    assert candidate == books.Candidate(
        title="The hobbit: or, There and back again",
        author="J. R. R. Tolkien",
        publisher="Houghton Mifflin",
        date="2012",
    )


def test_fetch_candidates_skips_failures(monkeypatch):
    """A provider that errors or misses is left out; the rest are kept in order."""

    async def found(_client, _isbn):
        return books.Candidate(title="The Hobbit", author="J.R.R. Tolkien")

    async def missing(_client, _isbn):
        return None

    async def broken(_client, _isbn):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        books, "PROVIDERS", {"a": broken, "b": found, "c": missing, "d": found}
    )
    candidates = asyncio.run(books.fetch_candidates(HOBBIT))
    assert list(candidates) == ["b", "d"]


CANDIDATES = {
    "isbndb": books.Candidate(title="The Hobbit (Paperback)", author="Tolkien, J.R.R."),
    "google": books.Candidate(title="The Hobbit", author="J. R. R. Tolkien"),
}


def stub_lookup(monkeypatch, merged, api_key="sk-test"):
    """Stub the providers, the Claude merge and the configured API key."""

    async def fetch(_isbn):
        return CANDIDATES

    async def merge(_isbn, _candidates):
        return merged

    monkeypatch.setattr(books, "fetch_candidates", fetch)
    monkeypatch.setattr(books, "merge_with_claude", merge)
    real_setting = books.setting
    monkeypatch.setattr(
        books,
        "setting",
        lambda option, fallback="": (
            api_key if option == "anthropic_api_key" else real_setting(option, fallback)
        ),
    )


def test_lookup_uses_claude_merge(monkeypatch):
    """Claude's merge wins and is cacheable; candidates are kept as JSON."""
    stub_lookup(
        monkeypatch,
        books.Merged(
            title="The Hobbit", author="J.R.R. Tolkien", confidence="high", notes=""
        ),
    )
    result = asyncio.run(books.lookup(HOBBIT))
    assert (result.title, result.author, result.source) == (
        "The Hobbit",
        "J.R.R. Tolkien",
        "claude",
    )
    assert result.confidence == "high" and result.cacheable
    assert set(json.loads(result.candidates)["sources"]) == {"isbndb", "google"}


def test_lookup_falls_back_uncached_when_claude_fails(monkeypatch):
    """With a key but no merge, the first provider is used and not cached."""
    stub_lookup(monkeypatch, None)
    result = asyncio.run(books.lookup(HOBBIT))
    assert (result.title, result.source, result.confidence) == (
        "The Hobbit (Paperback)",
        "isbndb",
        None,
    )
    assert not result.cacheable


def test_lookup_without_key_caches_fallback(monkeypatch):
    """Without Claude configured, the provider answer is final and cached."""
    stub_lookup(monkeypatch, None, api_key="")
    assert asyncio.run(books.lookup(HOBBIT)).cacheable


def test_clip_enforces_portal_limit():
    """Whitespace and control characters are cleaned; 100 characters max."""
    assert books.clip(" a\tb\n c ") == "a b c"
    assert len(books.clip("x" * 150)) == books.PORTAL_MAX_LENGTH


# --- Endpoint ----------------------------------------------------------------


def stub_endpoint_lookup(monkeypatch, result):
    """Make books.lookup return ``result`` and count its calls."""
    calls = []

    async def lookup(isbn):
        calls.append(isbn)
        return result

    monkeypatch.setattr(books, "lookup", lookup)
    return calls


def merged_result(cacheable=True):
    """A BookResult like a Claude merge returns."""
    return books.BookResult(
        title="The Hobbit",
        author="J.R.R. Tolkien",
        source="claude" if cacheable else "isbndb",
        confidence="high" if cacheable else None,
        candidates="{}",
        cacheable=cacheable,
    )


def test_get_book_rejects_invalid_isbn(client):
    """A bad ISBN is a 422 and never reaches the providers."""
    response = client.get("/books/9780547928228")
    assert response.status_code == 422
    assert response.json()["detail"] == "Not a valid ISBN."


def test_get_book_looks_up_and_caches(client, api_db, monkeypatch):
    """A miss is looked up (by ISBN-13) and cached; the next hit uses the cache."""
    calls = stub_endpoint_lookup(monkeypatch, merged_result())
    response = client.get("/books/0547928211")
    assert response.status_code == 200
    assert response.json() == {
        "isbn": "9780547928210",
        "title": "The Hobbit",
        "author": "J.R.R. Tolkien",
        "source": "claude",
        "confidence": "high",
    }
    assert client.get("/books/9780547928210").json()["title"] == "The Hobbit"
    assert calls == ["9780547928210"]
    with api_db() as session:
        assert session.get(models.Book, "9780547928210").source == "claude"


def test_get_book_does_not_cache_fallback(client, api_db, monkeypatch):
    """An uncacheable fallback is returned but looked up again next time."""
    calls = stub_endpoint_lookup(monkeypatch, merged_result(cacheable=False))
    assert client.get(f"/books/{HOBBIT}").json()["source"] == "isbndb"
    assert client.get(f"/books/{HOBBIT}").status_code == 200
    assert calls == [HOBBIT, HOBBIT]
    with api_db() as session:
        assert session.get(models.Book, HOBBIT) is None


def test_get_book_not_found(client, monkeypatch):
    """No provider knows the ISBN: 404."""
    stub_endpoint_lookup(monkeypatch, None)
    response = client.get(f"/books/{HOBBIT}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Book not found."


# --- Migration ---------------------------------------------------------------


def table_names(url):
    """Tables in the database at ``url``."""
    engine = sqlalchemy.create_engine(url)
    try:
        return set(sqlalchemy.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_books_migration_up_and_down(tmp_path):
    """The books migration creates the table and its downgrade drops it."""
    url = f"sqlite:///{tmp_path / 'books.db'}"
    config = db_bootstrap.build_alembic_config(url)
    command.upgrade(config, db_bootstrap.PRE_BOOKS_REVISION)
    assert "books" not in table_names(url)
    command.upgrade(config, "head")
    assert "books" in table_names(url)
    command.downgrade(config, db_bootstrap.PRE_BOOKS_REVISION)
    assert "books" not in table_names(url)


def test_bootstrap_adopts_pre_books_create_all_schema(tmp_path, monkeypatch):
    """A create_all schema from before books gets the books migration."""
    url = f"sqlite:///{tmp_path / 'prebooks.db'}"
    engine = sqlalchemy.create_engine(url)
    tables = [t for name, t in models.Base.metadata.tables.items() if name != "books"]
    models.Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        # Added after this schema's era.
        connection.execute(
            sqlalchemy.text("ALTER TABLE inmates DROP COLUMN next_request_index")
        )
    engine.dispose()

    monkeypatch.setattr(db_bootstrap, "get_sync_database_url", lambda: url)
    db_bootstrap.bootstrap()
    assert "books" in table_names(url)
