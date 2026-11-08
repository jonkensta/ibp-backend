"""UPC -> ISBN lookup: validation, UPCitemdb parsing, endpoints, migration."""

# pylint: disable=redefined-outer-name

import asyncio
import datetime

import httpx
import pytest
import sqlalchemy
from alembic import command

from ibp import books, db_bootstrap, models, upcs

# The real client, imported before conftest's api_db guard replaces it.
from ibp.upcs import barcodelookup as real_barcodelookup
from ibp.upcs import upcitemdb as real_upcitemdb

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


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"isbn": CRIME, "ean": CRIME}, CRIME),
        ({"isbn": "0140449132", "ean": "0012345678905"}, CRIME),  # ISBN-10
        ({"ean": CRIME}, CRIME),  # no isbn: a 978 EAN is one
        ({"isbn": "", "ean": "9791032305690"}, "9791032305690"),
        ({"ean": "0012345678905"}, None),  # not a Bookland EAN
        ({"isbn": "9780140449137"}, None),  # bad check digit
        ({}, None),
    ],
)
def test_item_isbn(item, expected):
    """The isbn field (10 or 13) wins; else a 978/979 EAN; else nothing."""
    assert upcs.item_isbn(item) == expected


def test_first_isbn_skips_items_without_one():
    """The first item that has an ISBN is used."""
    payload = {"code": "OK", "items": [{"ean": "0012345678905"}, {"isbn": CRIME}]}
    assert upcs.first_isbn(payload) == CRIME
    assert upcs.first_isbn({"code": "OK", "items": []}) is None


# --- UPCitemdb client --------------------------------------------------------


def mock_upcitemdb(monkeypatch, handler):
    """Route upcs' HTTP requests to ``handler``; return the requests seen."""
    requests = []
    real_client = httpx.AsyncClient

    def record(request):
        requests.append(request)
        return handler(request)

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(upcs.httpx, "AsyncClient", client)
    return requests


def ok(*items):
    """A UPCitemdb 200 response with ``items``."""
    return httpx.Response(
        200, json={"code": "OK", "total": len(items), "offset": 0, "items": items}
    )


def set_key(monkeypatch, key):
    """Configure ``[books] upcitemdb_key``."""
    real_setting = books.setting
    monkeypatch.setattr(
        upcs,
        "setting",
        lambda option, fallback="": (
            key if option == "upcitemdb_key" else real_setting(option, fallback)
        ),
    )


def test_upcitemdb_trial_endpoint(monkeypatch):
    """Without a key the trial endpoint is asked, with no key headers."""
    set_key(monkeypatch, "")
    requests = mock_upcitemdb(
        monkeypatch, lambda _r: ok({"ean": "0" + UPC, "isbn": CRIME})
    )
    assert asyncio.run(upcs.upcitemdb(UPC)) == CRIME
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{upcs.TRIAL_URL}?upc={UPC}"
    assert "user_key" not in request.headers


def test_upcitemdb_paid_endpoint(monkeypatch):
    """With a key the v1 endpoint is asked, the key in headers, not the URL."""
    set_key(monkeypatch, "secret")
    requests = mock_upcitemdb(monkeypatch, lambda _r: ok())
    assert asyncio.run(upcs.upcitemdb(UPC)) is None
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{upcs.PAID_URL}?upc={UPC}"
    assert request.headers["user_key"] == "secret"
    assert request.headers["key_type"] == "3scale"


@pytest.mark.parametrize("status", [400, 404])
def test_upcitemdb_invalid_or_unknown_is_a_miss(monkeypatch, status):
    """INVALID_UPC / not found answers are a definite miss."""
    set_key(monkeypatch, "")
    mock_upcitemdb(
        monkeypatch, lambda _r: httpx.Response(status, json={"code": "INVALID_UPC"})
    )
    assert asyncio.run(upcs.upcitemdb(UPC)) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"code": "EXCEED_LIMIT"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"code": "TOO_FAST"}),
    ],
)
def test_upcitemdb_unavailable(monkeypatch, response):
    """Rate limits, server errors and odd answers raise UpcServiceUnavailable."""
    set_key(monkeypatch, "")
    mock_upcitemdb(monkeypatch, lambda _r: response)
    with pytest.raises(upcs.UpcServiceUnavailable):
        asyncio.run(upcs.upcitemdb(UPC))


def test_upcitemdb_unreachable(monkeypatch):
    """A connection error raises UpcServiceUnavailable."""
    set_key(monkeypatch, "")

    def refuse(request):
        raise httpx.ConnectError("down", request=request)

    mock_upcitemdb(monkeypatch, refuse)
    with pytest.raises(upcs.UpcServiceUnavailable):
        asyncio.run(upcs.upcitemdb(UPC))


# --- Endpoints ---------------------------------------------------------------


def stub_service(monkeypatch, answer):
    """Make upcs.upcitemdb return (or raise) ``answer``; return the calls."""
    calls = []

    async def service(upc):
        calls.append(upc)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(upcs, "upcitemdb", service)
    return calls


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


def add_row(api_db, code, isbn, source="upcitemdb", age=datetime.timedelta(0)):
    """Seed a ``upcs`` row looked up ``age`` ago."""
    with api_db() as session:
        session.add(
            models.Upc(
                upc=code,
                isbn=isbn,
                source=source,
                looked_up_at=datetime.datetime.now(datetime.timezone.utc) - age,
            )
        )
        session.commit()


def get_row(api_db, code):
    """The ``upcs`` row for ``code`` as (isbn, source), or None."""
    with api_db() as session:
        row = session.get(models.Upc, code)
        return None if row is None else (row.isbn, row.source)


@pytest.mark.parametrize("code", [BAD_CHECK, "0" + UPC, UPC + "1", "abc"])
def test_get_rejects_invalid_code(client, monkeypatch, code):
    """A bad code is a 422 and never reaches the service."""
    calls = stub_service(monkeypatch, CRIME)
    response = client.get(f"/books/upc/{code}")
    assert response.status_code == 422
    assert not calls


def test_get_looks_up_caches_and_reuses(client, api_db, monkeypatch):
    """A new UPC asks the service (12 digits), caches it under the 12 digits."""
    calls = stub_service(monkeypatch, CRIME)
    book_calls = stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert response.json() == {
        "isbn": CRIME,
        "title": f"Title {CRIME}",
        "author": "Some Author",
        "source": "claude",
        "confidence": "high",
        "upc_source": "upcitemdb",
    }
    assert calls == [UPC]
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")
    assert get_row(api_db, UPC_ADD_ON) is None

    # Cache hit: neither the service nor the ISBN providers are asked again.
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["isbn"] == CRIME
    assert calls == [UPC]
    assert book_calls == [CRIME]


def test_get_cache_hit_skips_service(client, api_db, monkeypatch):
    """A cached mapping answers without asking the service."""
    add_row(api_db, UPC, HOBBIT, source="volunteer", age=datetime.timedelta(days=400))
    calls = stub_service(monkeypatch, AssertionError("not called"))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 200
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        HOBBIT,
        "volunteer",
    )
    assert not calls


def test_get_miss_is_cached(client, api_db, monkeypatch):
    """The service knowing no ISBN is a 404, cached as a miss."""
    calls = stub_service(monkeypatch, None)
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "No ISBN known for this UPC.",
        "reason": "no_isbn",
    }
    assert get_row(api_db, UPC) == (None, "upcitemdb")
    assert client.get(f"/books/upc/{UPC}").status_code == 404
    assert calls == [UPC]


def test_get_fresh_miss_skips_service(client, api_db, monkeypatch):
    """A miss younger than 30 days is a 404 without asking the service."""
    add_row(api_db, UPC, None, age=datetime.timedelta(days=29))
    calls = stub_service(monkeypatch, CRIME)
    assert client.get(f"/books/upc/{UPC}").status_code == 404
    assert not calls


def test_get_stale_miss_asks_again(client, api_db, monkeypatch):
    """A miss older than 30 days asks the service again and updates the row."""
    add_row(api_db, UPC, None, age=datetime.timedelta(days=31))
    calls = stub_service(monkeypatch, CRIME)
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 200
    assert response.json()["isbn"] == CRIME
    assert calls == [UPC]
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")


def test_get_service_unavailable_is_503_uncached(client, api_db, monkeypatch):
    """Rate-limited (or down): 503, and nothing is cached."""
    calls = stub_service(
        monkeypatch, upcs.UpcServiceUnavailable("UPC lookup service rate-limited.")
    )
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 503
    assert response.json()["detail"] == "UPC lookup service rate-limited."
    assert get_row(api_db, UPC) is None
    assert client.get(f"/books/upc/{UPC}").status_code == 503
    assert calls == [UPC, UPC]


def test_get_429_from_service_is_503(client, api_db, monkeypatch):
    """End to end through the client: UPCitemdb's 429 is a 503, uncached."""
    # The real client, against a mock transport.
    monkeypatch.setattr(upcs, "upcitemdb", real_upcitemdb)
    set_key(monkeypatch, "")
    mock_upcitemdb(
        monkeypatch, lambda _r: httpx.Response(429, json={"code": "EXCEED_LIMIT"})
    )
    assert client.get(f"/books/upc/{UPC}").status_code == 503
    assert get_row(api_db, UPC) is None


@pytest.mark.parametrize(
    "item",
    [
        {"isbn": CRIME},
        {"isbn": "0140449132"},
        {"ean": CRIME},
    ],
    ids=["isbn13", "isbn10", "ean-only"],
)
def test_get_service_item_shapes(client, api_db, monkeypatch, item):
    """The ISBN comes from an ISBN-13, an ISBN-10 or a Bookland EAN."""
    monkeypatch.setattr(upcs, "upcitemdb", real_upcitemdb)
    set_key(monkeypatch, "")
    mock_upcitemdb(monkeypatch, lambda _r: ok(item))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 200
    assert response.json()["isbn"] == CRIME
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")


def test_get_isbn_not_found_is_404(client, api_db, monkeypatch):
    """The UPC maps to an ISBN no provider knows: 404, mapping kept."""
    stub_service(monkeypatch, CRIME)
    stub_books(monkeypatch, known=())
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Book not found.",
        "reason": "no_book",
        "isbn": CRIME,
    }
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")


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
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": "0140449132"})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "volunteer")

    calls = stub_service(monkeypatch, AssertionError("not called"))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        CRIME,
        "volunteer",
    )
    assert not calls


def test_put_after_miss_teaches_full_code(client, api_db, monkeypatch):
    """After a core miss, the volunteer's ISBN goes under the full code."""
    calls = stub_service(monkeypatch, None)
    stub_books(monkeypatch)
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["reason"] == "no_isbn"
    assert get_row(api_db, UPC) == (None, "upcitemdb")
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (HOBBIT, "volunteer")
    assert get_row(api_db, UPC) == (None, "upcitemdb")

    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        HOBBIT,
        "volunteer",
    )
    # The other add-on still shares the core's (fresh) miss.
    assert client.get(f"/books/upc/{UPC_OTHER_ADD_ON}").status_code == 404
    assert calls == [UPC]


def test_put_replaces_full_code_miss(client, api_db):
    """A NULL-ISBN row under the full code is replaced (conditional UPDATE)."""
    add_row(api_db, UPC_ADD_ON, None)
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (HOBBIT, "volunteer")
    # The miss is gone: a second, different ISBN now conflicts.
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": CRIME})
    assert response.status_code == 409
    assert get_row(api_db, UPC_ADD_ON) == (HOBBIT, "volunteer")


def test_shared_core_add_ons(client, api_db, monkeypatch):
    """Two add-ons share the core's UPCitemdb answer, cached once."""
    calls = stub_service(monkeypatch, CRIME)
    stub_books(monkeypatch)
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["isbn"] == CRIME
    assert client.get(f"/books/upc/{UPC_OTHER_ADD_ON}").json()["isbn"] == CRIME
    assert calls == [UPC]
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")
    assert get_row(api_db, UPC_ADD_ON) is None
    assert get_row(api_db, UPC_OTHER_ADD_ON) is None


def test_volunteer_overrides_core_mapping(client, api_db, monkeypatch):
    """A core mapped to ISBN A doesn't block teaching ISBN B for core+add-on."""
    add_row(api_db, UPC, CRIME)
    stub_service(monkeypatch, AssertionError("not called"))
    stub_books(monkeypatch)
    response = client.put(f"/books/upc/{UPC_OTHER_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 204
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")

    response = client.get(f"/books/upc/{UPC_OTHER_ADD_ON}")
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        HOBBIT,
        "volunteer",
    )
    # The first title, and the bare UPC, still get the core's mapping.
    assert client.get(f"/books/upc/{UPC_ADD_ON}").json()["isbn"] == CRIME
    assert client.get(f"/books/upc/{UPC}").json()["isbn"] == CRIME


# --- Races: another request commits while UPCitemdb is being asked ---------


def stub_service_racing(monkeypatch, api_db, answer, winner):
    """The service answers ``answer`` after another request stores ``winner``.

    ``winner`` is the (code, isbn, source) of the row the other request commits.
    """

    async def service(_upc):
        add_or_update(api_db, *winner)
        return answer

    monkeypatch.setattr(upcs, "upcitemdb", service)


def add_or_update(api_db, code, isbn, source):
    """Commit a ``upcs`` row for ``code`` from a separate session."""
    with api_db() as session:
        row = session.get(models.Upc, code)
        if row is None:
            session.add(
                models.Upc(
                    upc=code,
                    isbn=isbn,
                    source=source,
                    looked_up_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
        else:
            row.isbn, row.source = isbn, source
        session.commit()


def test_get_insert_race_answers_from_winner(client, api_db, monkeypatch):
    """Losing the INSERT race, GET answers from the winning row."""
    stub_service_racing(monkeypatch, api_db, None, (UPC, HOBBIT, "upcitemdb"))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 200
    assert response.json()["isbn"] == HOBBIT
    assert get_row(api_db, UPC) == (HOBBIT, "upcitemdb")


def test_get_stale_miss_refresh_keeps_new_isbn(client, api_db, monkeypatch):
    """Refreshing an expired miss doesn't overwrite an ISBN stored meanwhile."""
    add_row(api_db, UPC, None, age=datetime.timedelta(days=31))
    stub_service_racing(monkeypatch, api_db, CRIME, (UPC, HOBBIT, "upcitemdb"))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC}")
    assert response.json()["isbn"] == HOBBIT
    assert get_row(api_db, UPC) == (HOBBIT, "upcitemdb")


def test_get_stale_miss_refresh_with_miss_keeps_new_isbn(client, api_db, monkeypatch):
    """A delayed miss doesn't erase an ISBN stored meanwhile either."""
    add_row(api_db, UPC, None, age=datetime.timedelta(days=31))
    stub_service_racing(monkeypatch, api_db, None, (UPC, HOBBIT, "upcitemdb"))
    stub_books(monkeypatch)
    assert client.get(f"/books/upc/{UPC}").json()["isbn"] == HOBBIT
    assert get_row(api_db, UPC) == (HOBBIT, "upcitemdb")


def test_get_prefers_volunteer_taught_meanwhile(client, api_db, monkeypatch):
    """A full-code mapping taught during the service call wins over a miss."""
    stub_service_racing(monkeypatch, api_db, None, (UPC_ADD_ON, HOBBIT, "volunteer"))
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert (response.json()["isbn"], response.json()["upc_source"]) == (
        HOBBIT,
        "volunteer",
    )
    assert get_row(api_db, UPC) == (None, "upcitemdb")


def test_write_upc_paths(api_db):
    """write_upc: insert, conditional replace of a miss, keep an existing ISBN."""
    # pylint: disable=import-outside-toplevel
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from ibp import book_api

    with api_db() as session:
        path = session.get_bind().url.database

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine) as session:
                inserted = await book_api.write_upc(session, UPC, None, "upcitemdb")
                replaced = await book_api.write_upc(session, UPC, CRIME, "upcitemdb")
                kept = await book_api.write_upc(session, UPC, HOBBIT, "volunteer")
                kept_miss = await book_api.write_upc(session, UPC, None, "upcitemdb")
            return inserted, replaced, kept, kept_miss
        finally:
            await engine.dispose()

    inserted, replaced, kept, kept_miss = asyncio.run(run())
    assert (inserted.isbn, replaced.isbn, kept.isbn, kept_miss.isbn) == (
        None,
        CRIME,
        CRIME,
        CRIME,
    )
    assert (kept.source, kept_miss.source) == ("upcitemdb", "upcitemdb")
    assert get_row(api_db, UPC) == (CRIME, "upcitemdb")


def test_put_conflict_keeps_mapping(client, api_db):
    """A UPC mapped to another ISBN is a 409 and keeps its mapping."""
    add_row(api_db, UPC_ADD_ON, CRIME)
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": HOBBIT})
    assert response.status_code == 409
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "upcitemdb")


def test_put_same_isbn_is_noop(client, api_db):
    """Putting the ISBN already mapped is a 204 that keeps the row's source."""
    add_row(api_db, UPC_ADD_ON, CRIME)
    response = client.put(f"/books/upc/{UPC_ADD_ON}", json={"isbn": CRIME})
    assert response.status_code == 204
    assert get_row(api_db, UPC_ADD_ON) == (CRIME, "upcitemdb")


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


# --- Barcode Lookup fallback -------------------------------------------------


def set_keys(monkeypatch, **keys):
    """Configure ``[books]`` options such as ``barcodelookup_key``."""
    real_setting = books.setting
    monkeypatch.setattr(
        upcs,
        "setting",
        lambda option, fallback="": keys.get(option, real_setting(option, fallback)),
    )


def product(**fields):
    """A Barcode Lookup 200 response with one product."""
    return httpx.Response(200, json={"products": [fields]})


@pytest.mark.parametrize(
    "fields",
    [
        {"barcode_formats": f"ISBN-10 0140449132, ISBN-13 {CRIME}"},
        {"barcode_formats": "ISBN-10 0140449132"},
        {"barcode_formats": "UPC-A 0" + CRIME[:11], "barcode_number": CRIME},
    ],
    ids=["formats-isbn13", "formats-isbn10", "barcode-number"],
)
def test_product_isbn(fields):
    """The ISBN comes from barcode_formats or a 978/979 barcode_number."""
    assert upcs.product_isbn(fields) == CRIME


def test_product_isbn_none_for_non_books():
    """A product with no ISBN anywhere has none."""
    assert (
        upcs.product_isbn({"barcode_formats": f"UPC-A {UPC}", "barcode_number": UPC})
        is None
    )


def test_barcodelookup_request_and_hit(monkeypatch):
    """The UPC and key go in the query string; the ISBN is parsed out."""
    requests = mock_upcitemdb(
        monkeypatch, lambda _r: product(barcode_formats=f"ISBN-13 {CRIME}")
    )
    assert asyncio.run(real_barcodelookup(UPC, "secret")) == CRIME
    assert str(requests[0].url) == f"{upcs.BARCODELOOKUP_URL}?barcode={UPC}&key=secret"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), product(barcode_number=UPC)],
    ids=["404", "no-isbn"],
)
def test_barcodelookup_unknown_is_a_miss(monkeypatch, response):
    """404 or a product without an ISBN is a miss."""
    mock_upcitemdb(monkeypatch, lambda _r: response)
    assert asyncio.run(real_barcodelookup(UPC, "k")) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="quota"),
        httpx.Response(429, text="slow down"),
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"error": "provider failure"}),
        httpx.Response(200, json={"products": "oops"}),
    ],
)
def test_barcodelookup_unavailable(monkeypatch, response):
    """Quota, server errors and bad JSON raise UpcServiceUnavailable."""
    mock_upcitemdb(monkeypatch, lambda _r: response)
    with pytest.raises(upcs.UpcServiceUnavailable):
        asyncio.run(real_barcodelookup(UPC, "k"))


def test_barcodelookup_error_hides_key(monkeypatch, caplog):
    """A connection error is logged by type only: httpx errors may quote the URL."""

    def refuse(request):
        raise httpx.ConnectError(f"down: {request.url}", request=request)

    mock_upcitemdb(monkeypatch, refuse)
    with pytest.raises(upcs.UpcServiceUnavailable) as raised:
        asyncio.run(real_barcodelookup(UPC, "secret"))
    assert "secret" not in caplog.text
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None


def stub_fallback(monkeypatch, answer):
    """Make upcs.barcodelookup return (or raise) ``answer``; return the calls."""
    calls = []

    async def service(upc, key):
        calls.append((upc, key))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(upcs, "barcodelookup", service)
    return calls


RATE_LIMITED = upcs.UpcServiceUnavailable("UPC lookup service rate-limited.")


def test_lookup_isbn_prefers_upcitemdb(monkeypatch):
    """A UPCitemdb answer, hit or miss, never touches the fallback."""
    set_keys(monkeypatch, barcodelookup_key="k")
    fallback = stub_fallback(monkeypatch, CRIME)
    stub_service(monkeypatch, None)
    assert asyncio.run(upcs.lookup_isbn(UPC)) == (None, "upcitemdb")
    stub_service(monkeypatch, HOBBIT)
    assert asyncio.run(upcs.lookup_isbn(UPC)) == (HOBBIT, "upcitemdb")
    assert not fallback


def test_lookup_isbn_without_key_reraises(monkeypatch):
    """No Barcode Lookup key: UPCitemdb's unavailability stands."""
    set_keys(monkeypatch, barcodelookup_key="")
    fallback = stub_fallback(monkeypatch, CRIME)
    stub_service(monkeypatch, RATE_LIMITED)
    with pytest.raises(upcs.UpcServiceUnavailable):
        asyncio.run(upcs.lookup_isbn(UPC))
    assert not fallback


def test_get_falls_back_to_barcodelookup(client, api_db, monkeypatch):
    """UPCitemdb rate-limited: Barcode Lookup answers and is cached as the source."""
    set_keys(monkeypatch, barcodelookup_key="k")
    stub_service(monkeypatch, RATE_LIMITED)
    fallback = stub_fallback(monkeypatch, CRIME)
    stub_books(monkeypatch)
    response = client.get(f"/books/upc/{UPC_ADD_ON}")
    assert response.status_code == 200
    assert response.json()["isbn"] == CRIME
    assert response.json()["upc_source"] == "barcodelookup"
    assert fallback == [(UPC, "k")]
    assert get_row(api_db, UPC) == (CRIME, "barcodelookup")


def test_get_fallback_miss_is_cached(client, api_db, monkeypatch):
    """A Barcode Lookup miss is a cached no_isbn 404 like a UPCitemdb one."""
    set_keys(monkeypatch, barcodelookup_key="k")
    stub_service(monkeypatch, RATE_LIMITED)
    stub_fallback(monkeypatch, None)
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 404
    assert response.json()["reason"] == "no_isbn"
    assert get_row(api_db, UPC) == (None, "barcodelookup")


def test_get_both_unavailable_is_503_uncached(client, api_db, monkeypatch):
    """Both services down: 503, nothing cached."""
    set_keys(monkeypatch, barcodelookup_key="k")
    stub_service(monkeypatch, RATE_LIMITED)
    stub_fallback(monkeypatch, upcs.UpcServiceUnavailable("UPC lookup service failed."))
    response = client.get(f"/books/upc/{UPC}")
    assert response.status_code == 503
    assert get_row(api_db, UPC) is None


def test_get_malformed_fallback_is_503_uncached(client, api_db, monkeypatch):
    """A Barcode Lookup 200 without a product list is a 503, not a cached miss."""
    set_keys(monkeypatch, barcodelookup_key="k")
    stub_service(monkeypatch, RATE_LIMITED)
    monkeypatch.setattr(upcs, "barcodelookup", real_barcodelookup)
    mock_upcitemdb(monkeypatch, lambda _r: httpx.Response(200, json={}))
    assert client.get(f"/books/upc/{UPC}").status_code == 503
    assert get_row(api_db, UPC) is None
