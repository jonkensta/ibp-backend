"""Portal registrations reported from the extension's scan mode."""

# pylint: disable=redefined-outer-name,unused-argument,unused-import

import pytest
from alembic import command

from ibp import db_bootstrap

from .test_package_items import (
    FEDERAL_PACKAGE,
    ISBN,
    ITEMS_URL,
    MANUAL_BOOK,
    OTHER_ISBN,
    PACKAGE,
    TOSSED,
    add,
    contents_revision,
    edit,
    registrations,
    seeded,
)

URL = f"/requests/{PACKAGE}/portal-registrations"

REPORT = {
    "isbn": ISBN,
    "title": "War and Peace",
    "author": "Tolstoy",
    "tdcj_number": "00012345",
    "confirmation_number": "1001",
}


def report(client, code=PACKAGE, **overrides):
    """POST a portal registration report."""
    return client.post(
        f"/requests/{code}/portal-registrations", json={**REPORT, **overrides}
    )


def test_new_book_is_recorded_registered(client, seeded):
    """With nothing recorded, the book is added already registered."""
    response = report(client)
    assert response.status_code == 201, response.text
    item = response.json()
    assert (item["isbn"], item["title"], item["author"], item["source"]) == (
        ISBN,
        "War and Peace",
        "Tolstoy",
        "isbn",
    )
    assert item["registered_at"] is not None
    assert item["confirmation_number"] == "1001"
    assert client.get(ITEMS_URL).json() == [item]
    assert contents_revision(seeded) == 1

    [row] = registrations(seeded)
    assert (row.item_id, row.title, row.tdcj_number, row.confirmation_number) == (
        item["id"],
        "War and Peace",
        "00012345",
        "1001",
    )
    assert row.registered_at.isoformat() == item["registered_at"]


def test_book_without_isbn_is_manual(client, seeded):
    """A report without an ISBN records a manual book."""
    item = report(client, isbn=None, title="A Zine", author="Unknown").json()
    assert (item["isbn"], item["source"]) == (None, "manual")


def test_matches_station_book_by_isbn(client, seeded):
    """A book the packing station recorded is registered, not duplicated."""
    recorded = add(client)
    revision = contents_revision(seeded)
    response = report(client)
    assert response.status_code == 201
    item = response.json()
    assert item["id"] == recorded["id"]
    assert item["revision"] == recorded["revision"]
    assert item["registered_at"] is not None
    assert len(client.get(ITEMS_URL).json()) == 1
    assert contents_revision(seeded) == revision


def test_matched_book_takes_submitted_text(client, seeded):
    """A title changed at the portal is what the book now says (an edit)."""
    recorded = add(client)
    revision = contents_revision(seeded)
    item = report(client, title="War & Peace").json()
    assert item["id"] == recorded["id"]
    assert (item["title"], item["source"]) == ("War & Peace", "manual")
    assert item["revision"] == recorded["revision"] + 1
    assert contents_revision(seeded) == revision + 1
    assert registrations(seeded)[0].title == "War & Peace"


def test_matches_by_title_without_isbn(client, seeded):
    """A station book without an ISBN matches by title and gets the ISBN."""
    recorded = add(client, **MANUAL_BOOK, isbn=None)
    item = report(client, title="a zine", author="Unknown").json()
    assert item["id"] == recorded["id"]
    assert (item["isbn"], item["title"], item["source"]) == (ISBN, "a zine", "manual")


@pytest.mark.parametrize(
    ("recorded", "reported"),
    [
        ("Hobbit, The", "The Hobbit"),
        ("War and Peace", "War & Peace"),
        ("Les Miserables", "Les Misérables"),
        ("don quixote!", "Don Quixote"),
    ],
)
def test_title_match_ignores_spelling(client, seeded, recorded, reported):
    """Case, accents, punctuation, '&' and the article's place don't matter."""
    station = add(client, **{**MANUAL_BOOK, "isbn": None, "title": recorded})
    item = report(client, title=reported).json()
    assert item["id"] == station["id"]
    assert item["title"] == reported
    assert len(client.get(ITEMS_URL).json()) == 1


@pytest.mark.parametrize(
    ("recorded", "reported"),
    [("Dune", "Dune Messiah"), ("The Hobbit", "The Hobbits")],
)
def test_title_match_needs_the_same_words(client, seeded, recorded, reported):
    """Another word, or a word added, is another book: no match."""
    station = add(client, **{**MANUAL_BOOK, "isbn": None, "title": recorded})
    item = report(client, title=reported).json()
    assert item["id"] != station["id"]
    assert len(client.get(ITEMS_URL).json()) == 2


def test_title_match_skips_another_isbn(client, seeded):
    """A station book with a different ISBN is another edition: no match."""
    add(client, isbn=OTHER_ISBN)
    report(client)
    assert len(client.get(ITEMS_URL).json()) == 2


def test_registered_books_are_not_matched(client, seeded):
    """A second copy of a registered book is recorded as a new book."""
    report(client)
    response = report(client, confirmation_number="1002")
    assert response.status_code == 201
    assert len(client.get(ITEMS_URL).json()) == 2
    assert [row.confirmation_number for row in registrations(seeded)] == [
        "1001",
        "1002",
    ]


def test_retry_is_idempotent(client, seeded):
    """The same confirmation number again returns the recorded book (200)."""
    first = report(client).json()
    revision = contents_revision(seeded)
    response = report(client)
    assert response.status_code == 200
    assert response.json() == first
    assert len(registrations(seeded)) == 1
    assert contents_revision(seeded) == revision


def test_same_confirmation_number_for_another_book(client, seeded):
    """Another book under a recorded confirmation number is refused, not dropped."""
    report(client)
    response = report(client, isbn=OTHER_ISBN, title="Another Book")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "That confirmation number is already recorded for another book."
    }
    assert len(registrations(seeded)) == 1
    assert len(client.get(ITEMS_URL).json()) == 1


def test_retry_after_book_deleted(client, seeded):
    """A retry for a book deleted since is refused; the record stays."""
    item = report(client).json()
    client.delete(f"{ITEMS_URL}/{item['id']}?confirm_registered=true")
    response = report(client)
    assert response.status_code == 409
    assert len(registrations(seeded)) == 1


def test_registered_book_is_read_only(client, seeded):
    """A book registered this way can't be edited, like any registered book."""
    item = report(client).json()
    assert edit(client, item, title="Changed").status_code == 409


@pytest.mark.parametrize(
    "code, overrides, detail",
    [
        (TOSSED, {}, "Request was tossed, so it has no package."),
        (
            FEDERAL_PACKAGE,
            {"tdcj_number": "07654321"},
            "Only Texas books are registered on the portal.",
        ),
        (
            PACKAGE,
            {"tdcj_number": "00000222"},
            "The portal shipment is for a different inmate.",
        ),
    ],
)
def test_refused(client, seeded, code, overrides, detail):
    """Tossed and federal requests, and another inmate's shipment, are 409."""
    response = report(client, code=code, **overrides)
    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert not registrations(seeded)


def test_unknown_request(client, seeded):
    """An unknown or malformed code is 404."""
    for code in ["TEX-00012345-9", "nonsense"]:
        assert report(client, code=code).status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"confirmation_number": ""},
        {"confirmation_number": "12 34"},
        {"confirmation_number": "1" * 33},
        {"tdcj_number": "12345"},
        {"title": "ab"},
        {"author": None},
        {"isbn": "123"},
    ],
)
def test_invalid_reports(client, seeded, overrides):
    """Bad fields are 422 and record nothing."""
    assert report(client, **overrides).status_code == 422
    assert not registrations(seeded)
    assert client.get(ITEMS_URL).json() == []


def test_confirmation_number_is_trimmed(client, seeded):
    """Whitespace around the confirmation number is dropped."""
    assert (
        report(client, confirmation_number=" 1001 ").json()["confirmation_number"]
        == "1001"
    )


def test_older_registration_has_no_confirmation_number(client, seeded):
    """The item registration endpoint leaves confirmation_number empty."""
    item = add(client)
    response = client.post(
        f"{ITEMS_URL}/{item['id']}/registrations", json={"revision": item["revision"]}
    )
    assert response.json()["confirmation_number"] is None


def test_migration_up_and_down(tmp_path):
    """The migration adds both confirmation_number columns; downgrade drops them."""
    url = f"sqlite:///{tmp_path / 'confirmation.db'}"
    config = db_bootstrap.build_alembic_config(url)

    def has_columns() -> set[bool]:
        _, columns = db_bootstrap.inspect_database(url)
        return {
            f"{table}.confirmation_number" in columns
            for table in ("package_items", "registrations")
        }

    command.upgrade(config, db_bootstrap.PRE_CONFIRMATION_NUMBER_REVISION)
    assert has_columns() == {False}
    command.upgrade(config, "head")
    assert has_columns() == {True}
    command.downgrade(config, db_bootstrap.PRE_CONFIRMATION_NUMBER_REVISION)
    assert has_columns() == {False}
