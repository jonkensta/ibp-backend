"""Package items: CRUD, revisions, registration and the registration queue."""

# pylint: disable=redefined-outer-name,unused-argument

import datetime

import pytest
from ibp import api, models

# Fresh, so the inmate has no entry-age warning.
FETCHED = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)

PACKAGE = "TEX-12345-0"
TOSSED = "TEX-12345-1"
OTHER_PACKAGE = "TEX-12345-2"
FEDERAL_PACKAGE = "FED-7654321-0"
OLDER_PACKAGE = "TEX-222-0"

ITEMS_URL = f"/requests/{PACKAGE}/items"

# ISBN-13 for ISBN-10 0-306-40615-2.
ISBN = "9780306406157"
OTHER_ISBN = "9781861972712"

BOOK = {"isbn": ISBN, "title": "War and Peace", "author": "Tolstoy", "source": "isbn"}
MANUAL_BOOK = {"title": "A Zine", "author": "Unknown", "source": "manual"}


@pytest.fixture
def seeded(api_db):
    """Seed Texas and Federal inmates with filled and tossed requests."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                city="Austin",
                state="TX",
                zipcode="78701",
                shipping_method="Individual",
            )
        )
        for jurisdiction, inmate_id, unit in [
            ("Texas", 12345, "Alpha"),
            ("Texas", 222, None),
            ("Federal", 7654321, None),
        ]:
            session.add(
                models.Inmate(
                    jurisdiction=jurisdiction,
                    id=inmate_id,
                    first_name="JANE",
                    last_name="DOE",
                    unit_name=unit,
                    datetime_fetched=FETCHED,
                    next_request_index=3,
                )
            )
        for jurisdiction, inmate_id, index, action, processed in [
            ("Texas", 12345, 0, "Filled", datetime.date(2026, 10, 2)),
            ("Texas", 12345, 1, "Tossed", datetime.date(2026, 10, 2)),
            ("Texas", 12345, 2, "Filled", datetime.date(2026, 10, 3)),
            ("Texas", 222, 0, "Filled", datetime.date(2026, 10, 1)),
            ("Federal", 7654321, 0, "Filled", datetime.date(2026, 9, 1)),
        ]:
            session.add(
                models.Request(
                    inmate_jurisdiction=jurisdiction,
                    inmate_id=inmate_id,
                    index=index,
                    action=action,
                    date_processed=processed,
                    date_postmarked=processed - datetime.timedelta(days=5),
                )
            )
    return api_db


def add(client, code=PACKAGE, **overrides) -> dict:
    """Add an item to a request and return the response body."""
    response = client.post(f"/requests/{code}/items", json={**BOOK, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def contents_revision(seeded, code=PACKAGE) -> int:
    """A request's contents_revision, read from the database."""
    jurisdiction = "Texas" if code.startswith("TEX") else "Federal"
    _, inmate_id, index = code.split("-")
    with seeded() as session:
        request = session.get(
            models.Request, (jurisdiction, int(inmate_id), int(index))
        )
        assert request is not None
        return request.contents_revision


def registrations(seeded) -> list[models.Registration]:
    """All registrations rows."""
    with seeded() as session:
        rows = session.query(models.Registration).order_by(models.Registration.id)
        session.expunge_all()
        return list(rows)


def register(client, item: dict, code=PACKAGE):
    """Report an item as registered at its current revision."""
    return client.post(
        f"/requests/{code}/items/{item['id']}/registrations",
        json={"revision": item["revision"]},
    )


def edit(client, item: dict, code=PACKAGE, **changes):
    """PUT an item with its current fields and revision, plus ``changes``."""
    body = {
        "isbn": item["isbn"],
        "title": item["title"],
        "author": item["author"],
        "revision": item["revision"],
        **changes,
    }
    return client.put(f"/requests/{code}/items/{item['id']}", json=body)


# --- CRUD -------------------------------------------------------------------


def test_add_item(client, seeded):
    """A new item is returned with its id, revision 0 and not registered."""
    item = add(client)
    assert {key: item[key] for key in BOOK} == BOOK
    assert item["revision"] == 0
    assert item["registered_at"] is None
    assert item["created_at"] == item["updated_at"]
    assert item["previously_sent"] == []
    assert isinstance(item["id"], int)


def test_list_items(client, seeded):
    """Items are listed oldest first, without previously_sent."""
    first = add(client)
    second = add(client, **MANUAL_BOOK, isbn=None)
    response = client.get(ITEMS_URL)
    assert response.status_code == 200
    listed = response.json()
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert set(listed[0]) == {
        "id",
        "isbn",
        "title",
        "author",
        "source",
        "revision",
        "registered_at",
        "created_at",
        "updated_at",
    }
    assert listed[1]["source"] == "manual"
    assert listed[1]["isbn"] is None


def test_edit_item(client, seeded):
    """An edit replaces the fields, bumps the revision and makes it manual."""
    item = add(client)
    response = edit(client, item, title="War & Peace", author="Unknown", isbn=None)
    assert response.status_code == 200
    edited = response.json()
    assert edited["title"] == "War & Peace"
    assert edited["author"] == "Unknown"
    assert edited["isbn"] is None
    assert edited["revision"] == 1
    assert edited["source"] == "manual"
    assert edited["updated_at"] > item["updated_at"]
    assert client.get(ITEMS_URL).json() == [edited]


def test_edit_isbn_only_keeps_source(client, seeded):
    """Changing only the ISBN doesn't make the title manual."""
    item = add(client)
    edited = edit(client, item, isbn=OTHER_ISBN).json()
    assert (edited["isbn"], edited["source"]) == (OTHER_ISBN, "isbn")


def test_delete_item(client, seeded):
    """A deleted item is gone."""
    item = add(client)
    response = client.delete(f"{ITEMS_URL}/{item['id']}")
    assert response.status_code == 204
    assert client.get(ITEMS_URL).json() == []


def test_item_ids_not_reused(client, seeded):
    """Deleting the newest item doesn't free its id (AUTOINCREMENT)."""
    add(client)
    newest = add(client)
    assert client.delete(f"{ITEMS_URL}/{newest['id']}").status_code == 204
    assert add(client)["id"] > newest["id"]


# --- Not found and wrong request state ---------------------------------------


@pytest.mark.parametrize("code", ["TEX-99999-0", "TEX-12345-9", "BAD", "TEX-x-0"])
@pytest.mark.parametrize(
    "method, suffix",
    [
        ("get", ""),
        ("get", "/items"),
        ("post", "/items"),
        ("put", "/items/1"),
        ("delete", "/items/1"),
        ("post", "/items/1/registrations"),
    ],
)
def test_unknown_request(client, seeded, code, method, suffix):
    """A malformed or unknown request code is 404 "Request not found."."""
    body = {"put": {**BOOK, "revision": 0}, "post": {**BOOK, "revision": 0}}
    kwargs = {"json": body[method]} if method in body else {}
    response = client.request(method, f"/requests/{code}{suffix}", **kwargs)
    assert response.status_code == 404
    assert response.json() == {"detail": "Request not found."}


@pytest.mark.parametrize(
    "method, suffix, body",
    [
        ("put", "", {**BOOK, "revision": 0}),
        ("delete", "", None),
        ("post", "/registrations", {"revision": 0}),
    ],
)
def test_item_of_another_request(client, seeded, method, suffix, body):
    """An item id that doesn't belong to the request is 404 "Item not found."."""
    item = add(client, code=OTHER_PACKAGE)
    response = client.request(method, f"{ITEMS_URL}/{item['id']}{suffix}", json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found."}
    response = client.request(method, f"{ITEMS_URL}/999{suffix}", json=body)
    assert response.json() == {"detail": "Item not found."}
    assert contents_revision(seeded) == 0


def test_tossed_request_has_no_package(client, seeded):
    """A tossed request: no package view, no item list, no items."""
    for url in [f"/requests/{TOSSED}", f"/requests/{TOSSED}/items"]:
        response = client.get(url)
        assert response.status_code == 409
        assert response.json()["detail"].endswith(".")
    response = client.post(f"/requests/{TOSSED}/items", json=BOOK)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Only filled requests can have books recorded."
    }
    assert contents_revision(seeded, TOSSED) == 0


# --- Revisions ----------------------------------------------------------------


def test_contents_revision_counts_every_change(client, seeded):
    """Add, edit and delete each increment contents_revision once."""
    item = add(client)
    assert contents_revision(seeded) == 1
    item = edit(client, item, title="New").json()
    assert contents_revision(seeded) == 2
    client.delete(f"{ITEMS_URL}/{item['id']}")
    assert contents_revision(seeded) == 3
    assert contents_revision(seeded, OTHER_PACKAGE) == 0


def test_contents_revision_in_responses(client, seeded):
    """The package view and the inmate's requests report contents_revision."""
    add(client)
    add(client)
    package = client.get(f"/requests/{PACKAGE}").json()
    assert package["request"]["contents_revision"] == 2
    inmate = client.get("/inmates/Texas/12345").json()
    by_code = {request["code"]: request for request in inmate["requests"]}
    assert by_code[PACKAGE]["contents_revision"] == 2


def test_stale_edit(client, seeded):
    """An edit based on an old revision is refused and changes nothing."""
    item = add(client)
    edit(client, item, title="First")
    response = edit(client, item, title="Second")
    assert response.status_code == 409
    assert response.json()["detail"].endswith(".")
    assert client.get(ITEMS_URL).json()[0]["title"] == "First"
    assert contents_revision(seeded) == 2


def test_concurrent_adds_keep_every_increment(seeded):
    """Two simultaneous adds to one request both count in contents_revision."""
    # pylint: disable=import-outside-toplevel
    import asyncio

    import httpx
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.util import await_only

    from ibp.base import app

    async def run() -> list[httpx.Response]:
        # Hold each add at the start of its transaction until both are there.
        barrier = asyncio.Barrier(2)
        waiting = {"left": 2}

        def after_begin(_session, _transaction, _connection):
            if waiting["left"]:
                waiting["left"] -= 1
                await_only(asyncio.wait_for(barrier.wait(), timeout=5))

        event.listen(Session, "after_begin", after_begin)
        try:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return list(
                    await asyncio.gather(
                        http.post(ITEMS_URL, json=BOOK),
                        http.post(ITEMS_URL, json=MANUAL_BOOK),
                    )
                )
        finally:
            event.remove(Session, "after_begin", after_begin)

    responses = asyncio.run(run())
    assert [response.status_code for response in responses] == [201, 201]
    assert contents_revision(seeded) == 2


# --- Registration -------------------------------------------------------------


def test_register_item(client, seeded):
    """Registering sets registered_at and appends a registrations row."""
    item = add(client)
    response = register(client, item)
    assert response.status_code == 201
    registered = response.json()
    assert registered["registered_at"] is not None
    assert registered["revision"] == item["revision"]
    assert contents_revision(seeded) == 1

    [row] = registrations(seeded)
    assert (row.item_id, row.title, row.author, row.tdcj_number) == (
        item["id"],
        "War and Peace",
        "Tolstoy",
        "00012345",
    )
    assert (row.inmate_jurisdiction, row.inmate_id, row.request_index) == (
        "Texas",
        12345,
        0,
    )
    assert row.registered_at.isoformat() == registered["registered_at"]


def test_register_stale_revision(client, seeded):
    """A book edited after the form was filled isn't marked registered."""
    item = add(client)
    edit(client, item, title="Changed")
    response = register(client, item)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "The book was changed since the form was filled."
    }
    assert client.get(ITEMS_URL).json()[0]["registered_at"] is None
    assert not registrations(seeded)


def test_register_twice(client, seeded):
    """A registered book can't be registered again."""
    item = add(client)
    register(client, item)
    response = register(client, item)
    assert response.status_code == 409
    assert response.json() == {"detail": "The book is already registered."}
    assert len(registrations(seeded)) == 1


def test_federal_items_are_not_registered(client, seeded):
    """Federal books are recorded but never registered on the portal."""
    item = add(client, code=FEDERAL_PACKAGE)
    response = register(client, item, code=FEDERAL_PACKAGE)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Only Texas books are registered on the portal."
    }
    assert not registrations(seeded)


def test_registered_item_is_read_only(client, seeded):
    """PUT on a registered item is refused, even with a current revision."""
    item = add(client)
    register(client, item)
    response = edit(client, item, title="Changed")
    assert response.status_code == 409
    assert response.json()["detail"].endswith(".")
    assert client.get(ITEMS_URL).json()[0]["title"] == "War and Peace"
    assert contents_revision(seeded) == 1


def test_delete_registered_item_needs_confirmation(client, seeded):
    """A registered item is deleted only when confirmed; its record stays."""
    item = add(client)
    register(client, item)
    url = f"{ITEMS_URL}/{item['id']}"

    response = client.delete(url)
    assert response.status_code == 409
    assert response.json()["detail"].endswith(".")
    assert len(client.get(ITEMS_URL).json()) == 1

    assert client.delete(url, params={"confirm_registered": "true"}).status_code == 204
    assert client.get(ITEMS_URL).json() == []
    [row] = registrations(seeded)
    assert row.item_id == item["id"]

    response = client.delete(f"/requests/{PACKAGE}")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Request has books registered on the portal, so it can't be deleted."
    }


def test_delete_request_with_items(client, seeded):
    """A request with books recorded can't be deleted until they are."""
    item = add(client)
    response = client.delete(f"/requests/{PACKAGE}")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Request has books recorded; delete them first."
    }

    client.delete(f"{ITEMS_URL}/{item['id']}")
    assert client.delete(f"/requests/{PACKAGE}").status_code == 204
    assert client.get(f"/requests/{PACKAGE}").status_code == 404


# --- Registration queue ------------------------------------------------------


def test_registration_queue(client, seeded):
    """Unregistered Texas items, grouped by request, oldest request first."""
    newer = add(client, code=OTHER_PACKAGE, title="Newer request")
    first = add(client, title="First")
    registered = add(client, title="Registered")
    second = add(client, title="Second", author="Unknown")
    older = add(client, code=OLDER_PACKAGE, title="Older request")
    add(client, code=FEDERAL_PACKAGE, title="Federal")
    register(client, registered)
    second = edit(client, second, title="Second, edited").json()

    response = client.get("/registration-queue")
    assert response.status_code == 200
    assert response.json() == [
        {
            "code": OLDER_PACKAGE,
            "item_id": older["id"],
            "revision": 0,
            "title": "Older request",
            "author": "Tolstoy",
            "tdcj_number": "00000222",
        },
        {
            "code": PACKAGE,
            "item_id": first["id"],
            "revision": 0,
            "title": "First",
            "author": "Tolstoy",
            "tdcj_number": "00012345",
        },
        {
            "code": PACKAGE,
            "item_id": second["id"],
            "revision": 1,
            "title": "Second, edited",
            "author": "Unknown",
            "tdcj_number": "00012345",
        },
        {
            "code": OTHER_PACKAGE,
            "item_id": newer["id"],
            "revision": 0,
            "title": "Newer request",
            "author": "Tolstoy",
            "tdcj_number": "00012345",
        },
    ]


def test_registration_queue_empty(client, seeded):
    """Nothing recorded, nothing to register."""
    assert client.get("/registration-queue").json() == []


# --- Validation ---------------------------------------------------------------


def test_title_and_author_are_cleaned(client, seeded):
    """Control characters are dropped and whitespace collapsed."""
    item = add(
        client,
        title="  War\tand\n\x00Peace\u200b  ",
        author=" Leo \x07 Tolstoy ",
    )
    assert (item["title"], item["author"]) == ("War and Peace", "Leo Tolstoy")


@pytest.mark.parametrize("author", [None, "", " \x01 "])
def test_author_is_required(client, seeded, author):
    """The portal needs an author: missing, blank or control-only is 422."""
    body = {**BOOK, "author": author}
    response = client.post(ITEMS_URL, json=body)
    assert response.status_code == 422
    del body["author"]
    assert client.post(ITEMS_URL, json=body).status_code == 422
    assert client.get(ITEMS_URL).json() == []


@pytest.mark.parametrize(
    "isbn, expected",
    [
        ("0-306-40615-2", ISBN),
        ("978-0-306-40615-7", ISBN),
        ("978030640615751299", ISBN),
        ("", None),
    ],
)
def test_isbn_is_normalized(client, seeded, isbn, expected):
    """ISBN-10 becomes ISBN-13; a price add-on is trimmed; blank is none."""
    assert add(client, isbn=isbn, source="manual")["isbn"] == expected


@pytest.mark.parametrize("field", ["title", "author"])
@pytest.mark.parametrize(
    "length, accepted", [(2, False), (3, True), (100, True), (101, False)]
)
def test_length_limits(client, seeded, field, length, accepted):
    """Title and author take 3 to 100 characters, as the portal requires."""
    response = client.post(ITEMS_URL, json={**BOOK, field: "x" * length})
    if accepted:
        assert response.status_code == 201
        assert len(response.json()[field]) == length
    else:
        assert response.status_code == 422
        assert response.json()["detail"][0]["msg"] == "Must be 3 to 100 characters."


def test_length_counts_cleaned_text(client, seeded):
    """Collapsed whitespace doesn't count; padding doesn't make it long enough."""
    assert len(add(client, title="T" * 50 + "     " + "T" * 49)["title"]) == 100
    response = client.post(ITEMS_URL, json={**BOOK, "author": "  A \x00 B  "})
    assert response.status_code == 201
    assert response.json()["author"] == "A B"
    response = client.post(ITEMS_URL, json={**BOOK, "author": " AB \x00 "})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "T" * 101},
        {"author": "A" * 101},
        {"author": "AB"},
        {"author": None},
        {"title": ""},
        {"title": " \t\x00 "},
        {"title": None},
        {"isbn": "9780306406158"},
        {"isbn": "12345"},
        {"source": "claude"},
        {"isbn": None, "source": "isbn"},
    ],
)
def test_invalid_items_are_refused(client, seeded, overrides):
    """Invalid fields are 422 and record nothing."""
    response = client.post(ITEMS_URL, json={**BOOK, **overrides})
    assert response.status_code == 422
    assert client.get(ITEMS_URL).json() == []
    assert contents_revision(seeded) == 0


def test_bad_isbn_message(client, seeded):
    """A bad ISBN says so."""
    response = client.post(ITEMS_URL, json={**BOOK, "isbn": "12345"})
    assert response.json()["detail"][0]["msg"] == "Not a valid ISBN."


def test_edits_are_validated(client, seeded):
    """PUT applies the same cleanup and limits."""
    item = add(client)
    assert edit(client, item, title="T" * 101).status_code == 422
    assert edit(client, item, isbn="bad").status_code == 422
    assert edit(client, item, title="TT").status_code == 422
    assert edit(client, item, author=None).status_code == 422
    assert edit(client, item, author="  ").status_code == 422
    edited = edit(client, item, title=" Two\n words ", isbn="0306406152").json()
    assert (edited["title"], edited["isbn"]) == ("Two words", ISBN)


# --- Book cache ---------------------------------------------------------------


@pytest.fixture
def cached_book(seeded):
    """A cached lookup for ISBN, as the providers answered it."""
    with seeded.begin() as session:
        session.add(
            models.Book(
                isbn=ISBN,
                title="War and Peace (Vintage Classics)",
                author="Leo Tolstoy",
                source="claude",
                confidence="high",
                candidates='[{"provider": "isbndb"}]',
                fetched_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            )
        )


def cached(seeded, isbn=ISBN):
    """The books cache row for ``isbn`` (or None)."""
    with seeded() as session:
        book = session.get(models.Book, isbn)
        session.expunge_all()
        return book


def test_edit_leaves_cache_alone(client, cached_book, seeded):
    """Editing an item doesn't change the ISBN cache by default."""
    item = add(client)
    edit(client, item, title="War and Peace, abridged")
    book = cached(seeded)
    assert (book.title, book.source) == ("War and Peace (Vintage Classics)", "claude")


def test_edit_updates_cache_when_asked(client, cached_book, seeded):
    """update_cache saves the title for future scans and keeps the candidates."""
    item = add(client)
    response = edit(
        client, item, title="War and Peace", author="Tolstoy", update_cache=True
    )
    assert response.status_code == 200
    book = cached(seeded)
    assert (book.title, book.author, book.source, book.confidence) == (
        "War and Peace",
        "Tolstoy",
        "manual",
        None,
    )
    assert book.candidates == '[{"provider": "isbndb"}]'
    assert book.fetched_at > datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
    assert client.get(f"/books/{ISBN}").json()["title"] == "War and Peace"


def test_update_cache_inserts_missing_row(client, seeded):
    """update_cache adds a cache row when the ISBN wasn't cached."""
    item = add(client, isbn=OTHER_ISBN, source="manual")
    edit(client, item, title="Typed Title", update_cache=True)
    book = cached(seeded, OTHER_ISBN)
    assert (book.title, book.author, book.source) == (
        "Typed Title",
        "Tolstoy",
        "manual",
    )


def test_update_cache_without_isbn(client, seeded):
    """update_cache without an ISBN changes nothing in the cache."""
    item = add(client, **MANUAL_BOOK, isbn=None)
    assert edit(client, item, title="New", update_cache=True).status_code == 200
    with seeded() as session:
        assert session.query(models.Book).count() == 0


def test_refused_edit_leaves_cache_alone(client, cached_book, seeded):
    """A stale edit doesn't reach the cache either."""
    item = add(client)
    edit(client, item, title="First")
    assert edit(client, item, title="Second", update_cache=True).status_code == 409
    assert cached(seeded).source == "claude"


# --- Previously sent ------------------------------------------------------------


def test_previously_sent(client, seeded):
    """Adding a book lists this inmate's other requests that already have it."""
    assert add(client, code=OTHER_PACKAGE)["previously_sent"] == []
    add(client, code=OLDER_PACKAGE)  # another inmate: not listed
    assert add(client)["previously_sent"] == [OTHER_PACKAGE]
    # The same request isn't listed, and each request is listed once.
    add(client, code=OTHER_PACKAGE)
    assert add(client)["previously_sent"] == [OTHER_PACKAGE]
    assert add(client, isbn=OTHER_ISBN)["previously_sent"] == []
    assert add(client, **MANUAL_BOOK, isbn=None)["previously_sent"] == []


# --- Package view -------------------------------------------------------------


def test_get_package(client, seeded, monkeypatch):
    """The package view: request, inmate with unit, items and inmate warnings."""

    def no_upsert(*_args, **_kwargs):
        raise AssertionError("provider upsert must not be called")

    monkeypatch.setattr(api, "upsert_inmates_by_inmate_id", no_upsert)
    item = add(client)

    response = client.get(f"/requests/{PACKAGE.lower()}")
    assert response.status_code == 200
    package = response.json()
    assert set(package) == {"request", "inmate", "items", "warnings"}
    assert package["request"] == {
        "date_processed": "2026-10-02",
        "date_postmarked": "2026-09-27",
        "action": "Filled",
        "request_id": None,
        "index": 0,
        "code": PACKAGE,
        "contents_revision": 1,
    }
    assert package["inmate"]["id"] == 12345
    assert package["inmate"]["first_name"] == "JANE"
    assert package["inmate"]["unit"]["name"] == "Alpha"
    assert "requests" not in package["inmate"]
    assert [entry["id"] for entry in package["items"]] == [item["id"]]
    assert package["warnings"] == {"entry_age": None, "release": None}

    with seeded() as session:
        assert session.query(models.Lookup).count() == 0


def test_get_package_warnings(client, seeded):
    """Inmate warnings are included; no postmark warning about the request."""
    with seeded.begin() as session:
        inmate = session.get(models.Inmate, ("Texas", 12345))
        inmate.datetime_fetched = None
    warnings = client.get(f"/requests/{PACKAGE}").json()["warnings"]
    assert warnings["entry_age"].startswith("Data entry for Texas inmate #00012345")
    assert set(warnings) == {"entry_age", "release"}
