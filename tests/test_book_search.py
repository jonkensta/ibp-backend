"""Title and author suggestions: normalization, providers, guard, cache."""

# pylint: disable=redefined-outer-name

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from ibp import book_search, books
from ibp.base import config
from ibp.book_search import Candidate, Judgment


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An empty cache, an Anthropic key, and no live provider or Claude call."""
    book_search.CACHE.clear()
    settings = {"anthropic_api_key": "sk-test"}
    monkeypatch.setattr(
        book_search, "setting", lambda option, fallback="": settings.get(option, "")
    )

    async def no_providers(*_args):
        raise AssertionError("providers must not be queried")

    async def no_claude(*_args):
        raise AssertionError("Claude must not be asked")

    monkeypatch.setattr(book_search, "fetch_search_candidates", no_providers)
    monkeypatch.setattr(book_search, "judge_with_claude", no_claude)
    yield settings
    book_search.CACHE.clear()


# --- Normalization -------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Hobbit", "hobbit"),
        ("Hobbit, The", "hobbit"),
        ("THE  HOBBIT!", "hobbit"),
        ("Man’s Search for Meaning", "mans search for meaning"),
        ("Man's Search for Meaning", "mans search for meaning"),
        ("Of Mice & Men", "of mice and men"),
        ("Les Misérables", "les miserables"),
        ("Moby-Dick", "moby dick"),
        ("A Raisin in the Sun", "raisin in the sun"),
        ("A", "a"),
    ],
)
def test_normalize_title(title, expected):
    """Case, diacritics, punctuation, '&' and articles are set aside."""
    assert book_search.normalize_title(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Man's Search for Meaning: An Introduction to Logotherapy", "mans search for meaning"),
        ("The hobbit, or, There and back again", "hobbit"),
        ("Frankenstein; or, The Modern Prometheus", "frankenstein"),
        ("The Hobbit (Paperback)", "hobbit"),
        ("Man's Search for Meaning adapted for Young Adults [adaptation]",
         "mans search for meaning adapted for young adults"),
        ("Catch-22", "catch 22"),
    ],
)  # fmt: skip
def test_normalize_main_title(title, expected):
    """The main title stops at a subtitle and drops a trailing note."""
    assert book_search.normalize_main_title(title) == expected


@pytest.mark.parametrize(
    ("author", "normalized", "surname"),
    [
        ("J.R.R. Tolkien", "tolkien", "tolkien"),
        ("Tolkien, J. R. R.", "tolkien", "tolkien"),
        ("Frankl, Viktor E. (Viktor Emil)", "viktor frankl", "frankl"),
        ("Viktor Emil Frankl", "viktor emil frankl", "frankl"),
        ("Martin Luther King, Jr.", "martin luther king", "king"),
        ("King, Martin Luther, Jr.", "king martin luther", "king"),
        ("William Strunk Jr. and E. B. White", "william strunk white", "strunk"),
        ("Gabriel García Márquez", "gabriel garcia marquez", "marquez"),
        ("Hurston, Zora Neale", "zora neale hurston", "hurston"),
        ("Zora Neale Hurston, Henry Louis Gates","zora neale hurston henry louis gates",
         "hurston"),
        ("", "", ""),
    ],
)  # fmt: skip
def test_normalize_author(author, normalized, surname):
    """Name order, periods and initials are set aside; the surname is kept."""
    assert book_search.normalize_author(author) == normalized
    assert book_search.surname(author) == surname


def test_authors_match():
    """Order, initials and a dropped middle name don't count; a new name does."""
    assert book_search.authors_match("Frankl, Viktor", "Viktor E. Frankl")
    assert book_search.authors_match("Viktor Frankl", "Viktor Emil Frankl")
    assert book_search.authors_match("tolkien", "J. R. R. Tolkien")
    assert not book_search.authors_match("Steven King", "Stephen King")
    assert not book_search.authors_match("Tolkein", "Tolkien")
    assert not book_search.authors_match("", "")


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [
        ("kitten", "sitting", 3),
        ("tolkein", "tolkien", 1),
        ("", "abc", 3),
        ("ab", "ab", 0),
    ],
)
def test_edit_distance(a, b, distance):
    """Levenshtein distance, with a swap of neighbours as one edit."""
    assert book_search.edit_distance(a, b) == distance


def test_presets():
    """IBP's own items are recognized however they are typed."""
    assert book_search.is_preset("resource guide")
    assert book_search.is_preset("Self Care on the Inside")
    assert not book_search.is_preset("The Resource Guide to Prison Law")


# --- Providers -----------------------------------------------------------------


def run_provider(provider, handler, title="The Hobbit", author="J.R.R. Tolkien"):
    """Run a search provider against an httpx mock transport."""

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider(client, title, author)

    return asyncio.run(run())


def json_handler(body, requests):
    """A handler answering ``body`` as JSON and recording each request."""

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    return handle


def never_called(_request):
    """A handler for a provider that must not send a request."""
    raise AssertionError("no request expected")


OPEN_LIBRARY_JSON = {
    "numFound": 2,
    "docs": [
        {
            "title": "Man's Search for Meaning",
            "subtitle": "An Introduction to Logotherapy",
            "author_name": ["Viktor Emil Frankl"],
            "publisher": ["Washington Square Pr", "Beacon Press"],
            "first_publish_year": 1968,
        },
        {"title": "Man's Search for Ultimate Meaning", "author_name": ["Viktor Emil Frankl"]},
        {"author_name": ["No Title"]},
    ],
}  # fmt: skip


def test_open_library_search():
    """Title plus author, and title alone; subtitles are joined on."""
    requests: list[httpx.Request] = []
    handler = json_handler(OPEN_LIBRARY_JSON, requests)
    found = run_provider(book_search.open_library_search, handler)
    assert found[0] == Candidate(
        title="Man's Search for Meaning: An Introduction to Logotherapy",
        author="Viktor Emil Frankl",
        publisher="Washington Square Pr",
        date="1968",
    )
    assert [c.title for c in found] == [
        "Man's Search for Meaning: An Introduction to Logotherapy",
        "Man's Search for Ultimate Meaning",
    ]
    run_provider(book_search.open_library_title_search, handler)
    assert requests[0].url.params["title"] == "The Hobbit"
    assert requests[0].url.params["author"] == "J.R.R. Tolkien"
    assert "author" not in requests[1].url.params
    assert requests[0].headers["User-Agent"] != ""


LOC_XML = """<?xml version="1.0"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
<zs:numberOfRecords>3</zs:numberOfRecords><zs:records>
<zs:record><zs:recordData><mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo><title>Man's search for meaning</title>
    <subTitle>an introduction to logotherapy</subTitle></titleInfo>
  <titleInfo type="uniform"><title>Psycholog erlebt das Konzentrationslager. English</title>
  </titleInfo>
  <name type="personal" usage="primary">
    <namePart>Frankl, Viktor E. (Viktor Emil),</namePart>
    <namePart type="date">1905-1997</namePart></name>
  <originInfo><dateIssued encoding="marc">1984</dateIssued></originInfo>
</mods></zs:recordData></zs:record>
<zs:record><zs:recordData><mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo><title>Man's search for ultimate meaning</title></titleInfo>
  <name type="personal" usage="primary"><namePart>Frankl, Viktor E.</namePart></name>
</mods></zs:recordData></zs:record>
<zs:record><zs:recordData><mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo type="uniform"><title>No main title</title></titleInfo>
</mods></zs:recordData></zs:record>
</zs:records></zs:searchRetrieveResponse>"""


def test_library_of_congress_search():
    """Every MODS record is read; the query is a CQL title and creator."""
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=LOC_XML)

    found = run_provider(
        book_search.library_of_congress_search, handler, title='Man\'s "Search"*'
    )
    assert [(c.title, c.author, c.date) for c in found] == [
        (
            "Man's search for meaning: an introduction to logotherapy",
            "Viktor E. (Viktor Emil) Frankl",
            "1984",
        ),
        ("Man's search for ultimate meaning", "Viktor E. Frankl", ""),
    ]
    assert (
        requests[0].url.params["query"]
        == 'dc.title="Man\'s Search" and dc.creator="J.R.R. Tolkien"'
    )


def test_library_of_congress_no_records():
    """A response without records (or a CQL diagnostic) finds nothing."""
    empty = "<zs:searchRetrieveResponse xmlns:zs='http://www.loc.gov/zing/srw/'/>"
    found = run_provider(
        book_search.library_of_congress_search,
        lambda _request: httpx.Response(200, text=empty),
    )
    assert not found


ISBNDB_JSON = {
    "total": 1,
    "data": [
        {
            "title": "The Hobbit",
            "title_long": "The Hobbit (Paperback)",
            "authors": ["Tolkien, J.R.R."],
            "publisher": "Mariner",
            "date_published": 2012,
        },
        {"authors": ["No Title"]},
    ],
}


def test_isbndb_search(isolated):
    """ISBNdb's field search sends the key in a header and reads ``data``."""
    isolated["isbndb_key"] = "isbndb-test"
    requests: list[httpx.Request] = []
    found = run_provider(book_search.isbndb_search, json_handler(ISBNDB_JSON, requests))
    assert found == [
        Candidate(
            title="The Hobbit (Paperback)",
            author="J.R.R. Tolkien",
            publisher="Mariner",
            date="2012",
        )
    ]
    assert requests[0].headers["Authorization"] == "isbndb-test"
    assert requests[0].url.params["text"] == "The Hobbit"
    assert requests[0].url.params["author"] == "J.R.R. Tolkien"


GOOGLE_JSON = {
    "items": [
        {
            "volumeInfo": {
                "title": "The Hobbit",
                "subtitle": "Or There and Back Again",
                "authors": ["J. R. R. Tolkien"],
                "publishedDate": "2012-09-18",
            }
        },
        {"volumeInfo": {}},
    ]
}


def test_google_search(isolated):
    """Google is asked intitle/inauthor with the key."""
    isolated["google_books_key"] = "google-test"
    requests: list[httpx.Request] = []
    found = run_provider(book_search.google_search, json_handler(GOOGLE_JSON, requests))
    assert [(c.title, c.author) for c in found] == [
        ("The Hobbit: Or There and Back Again", "J. R. R. Tolkien")
    ]
    assert requests[0].url.params["q"] == "intitle:The Hobbit inauthor:J.R.R. Tolkien"
    assert requests[0].url.params["key"] == "google-test"


@pytest.mark.parametrize("provider", ["isbndb_search", "google_search"])
def test_provider_without_key_is_skipped(provider):
    """ISBNdb and Google send nothing without their key."""
    assert not run_provider(getattr(book_search, provider), never_called)


def test_fetch_search_candidates(monkeypatch):
    """Results are pooled with their provider names, in provider order."""

    def found(title):
        async def provider(_client, _title, _author):
            return [Candidate(title=title)]

        return provider

    async def missing(_client, _title, _author):
        return []

    monkeypatch.undo()
    monkeypatch.setattr(
        book_search,
        "SEARCH_PROVIDERS",
        {"a": found("One"), "b": missing, "c": found("Two")},
    )
    result = asyncio.run(book_search.fetch_search_candidates("t", "a"))
    assert result == [("a", Candidate(title="One")), ("c", Candidate(title="Two"))]


def test_fetch_search_candidates_provider_error(monkeypatch, caplog):
    """Any provider failing is None (the key is redacted from the log)."""

    async def broken(_client, _title, _author):
        raise httpx.ConnectError("down at https://x/?key=secret")

    async def found(_client, _title, _author):
        return [Candidate(title="One")]

    monkeypatch.undo()
    monkeypatch.setattr(book_search, "SEARCH_PROVIDERS", {"a": found, "b": broken})
    assert asyncio.run(book_search.fetch_search_candidates("t", "a")) is None
    assert "secret" not in caplog.text and "key=***" in caplog.text


# --- Guard ---------------------------------------------------------------------

# A small catalog of classics and traps (sequels, adaptations, near-namesakes,
# a catalog typo), as the providers might return them.
CATALOG = [
    ("The Hobbit: or, There and Back Again", "J. R. R. Tolkien"),
    ("The Hobbit", "J.R.R. Tolkien"),
    ("The Hobbit", "Charles Dixon, Sean Deming, J.R.R. Tolkien"),
    ("The Annotated Hobbit", "J. R. R. Tolkien"),
    ("The Lord of the Rings", "J. R. R. Tolkien"),
    ("Man's Search for Meaning", "Viktor E. Frankl"),
    ("Man's Search for Meaning: An Introduction to Logotherapy", "Viktor Emil Frankl"),
    ("Man's Search for Ultimate Meaning", "Viktor Emil Frankl"),
    ("Man's Search for Meaning adapted for Young Adults [adaptation]", "Viktor Emil Frankl"),
    ("Dune", "Frank Herbert"),
    ("Dune Messiah", "Frank Herbert"),
    ("Children of Dune", "Frank Herbert"),
    ("To Kill a Mockingbird", "Harper Lee"),
    ("Go Set a Watchman", "Harper Lee"),
    ("Of Mice and Men", "John Steinbeck"),
    ("The Grapes of Wrath", "John Steinbeck"),
    ("The Grapes of Wroth", "John Steinbeck"),  # a catalog typo
    ("One Hundred Years of Solitude", "Gabriel García Márquez"),
    ("Their Eyes Were Watching God", "Zora Neale Hurston"),
    ("Invisible Man", "Ralph Ellison"),
    ("The Invisible Man", "H. G. Wells"),
    ("Beloved", "Toni Morrison"),
    ("Night", "Elie Wiesel"),
    ("The Count of Monte Cristo (Abridged)", "Alexandre Dumas"),
    ("The Three Musketeers", "Alexandre Dumas"),
    ("Frankenstein; or, The Modern Prometheus", "Mary Wollstonecraft Shelley"),
    ("The Adventures of Huckleberry Finn", "Mark Twain"),
    ("The Adventures of Tom Sawyer", "Mark Twain"),
    ("Les Misérables", "Victor Hugo"),
    ("The Art of War", "Sun Tzu"),
    ("The Art of Racing in the Rain", "Garth Stein"),
    ("The 48 Laws of Power", "Robert Greene"),
    ("The 33 Strategies of War", "Robert Greene"),
    ("The Catcher in the Rye", "J. D. Salinger"),
    ("Siddhartha", "Hermann Hesse"),
    ("Steppenwolf", "Hermann Hesse"),
    ("Ender's Game", "Orson Scott Card"),
    ("Ender's Shadow", "Orson Scott Card"),
    ("Carrie", "Stephen King"),
    ("Cujo", "Stephen King"),
    ("Moby-Dick; or, The Whale", "Herman Melville"),
    ("Catch-22", "Joseph Heller"),
    ("Lord of the Flies", "William Golding"),
    ("1984", "George Orwell"),
    ("Animal Farm", "George Orwell"),
]  # fmt: skip


def catalog_groups(title, author, catalog=None):
    """The catalog, grouped and ranked against the typed text."""
    found = [("catalog", Candidate(title=t, author=a)) for t, a in catalog or CATALOG]
    return book_search.group_candidates(title, author, found)


def claude_says(groups, title, author, verdict="same_book", confidence="high"):
    """Claude choosing the group that holds (title, author)."""
    main, last = book_search.normalize_main_title(title), book_search.surname(author)
    chosen = next((g for g in groups if (g.main, g.surname) == (main, last)), None)
    chosen = chosen or next((g for g in groups if g.main == main), None)
    return Judgment(
        verdict=verdict,
        candidate_id=chosen.id if chosen else "c99",
        title=title,
        author=author,
        confidence=confidence,
        notes="",
    )


def guard(typed, suggested, **judgment):
    """Run the guard for a typed and a suggested (title, author)."""
    groups = catalog_groups(*typed)
    return book_search.is_confident_suggestion(
        *typed, claude_says(groups, *suggested, **judgment), groups
    )


# (typed title, typed author), (Claude's title, Claude's author), suggest?
# pylint: disable=line-too-long
EVALUATION = [
    # Typos fixed.
    (("The Hobit", "J.R.R. Tolkien"), ("The Hobbit", "J.R.R. Tolkien"), True),
    (("Mans Serch for Meaning", "Viktor Frankl"), ("Man's Search for Meaning", "Viktor E. Frankl"), True),
    # A subtitle may come along with a real fix.
    (("Mans Serch for Meaning", "Viktor Frankl"),
     ("Man's Search for Meaning: An Introduction to Logotherapy", "Viktor Emil Frankl"), True),
    (("Dune Mesiah", "Frank Herbert"), ("Dune Messiah", "Frank Herbert"), True),
    (("To Kil a Mockingbird", "Harper Lee"), ("To Kill a Mockingbird", "Harper Lee"), True),
    (("One Hundred Years of Solitud", "Gabriel Garcia Marquez"),
     ("One Hundred Years of Solitude", "Gabriel García Márquez"), True),
    (("Their Eyes Where Watching God", "Zora Neale Hurston"),
     ("Their Eyes Were Watching God", "Zora Neale Hurston"), True),
    (("Belovd", "Toni Morrison"), ("Beloved", "Toni Morrison"), True),
    # A swap of neighbours in a 5-letter title: difflib ratio 0.80, which is
    # why MIN_TITLE_RATIO is 0.80, not 0.85.
    (("Nigth", "Elie Wiesel"), ("Night", "Elie Wiesel"), True),
    (("Count of Monte Christo", "Alexandre Dumas"), ("The Count of Monte Cristo", "Alexandre Dumas"), True),
    (("Frankenstien", "Mary Shelley"), ("Frankenstein", "Mary Shelley"), True),
    (("Adventures of Huckleberry Fin", "Mark Twain"),
     ("The Adventures of Huckleberry Finn", "Mark Twain"), True),
    (("Les Miserable", "Victor Hugo"), ("Les Misérables", "Victor Hugo"), True),
    (("The Art of Wor", "Sun Tzu"), ("The Art of War", "Sun Tzu"), True),
    (("The Cather in the Rye", "J. D. Salinger"), ("The Catcher in the Rye", "J. D. Salinger"), True),
    (("Siddartha", "Herman Hesse"), ("Siddhartha", "Hermann Hesse"), True),
    (("Moby Dik", "Herman Melvile"), ("Moby-Dick", "Herman Melville"), True),
    # Author fixed, title right.
    (("The Hobbit", "J.R.R. Tolkein"), ("The Hobbit", "J.R.R. Tolkien"), True),
    (("Grapes of Wrath", "John Steinback"), ("The Grapes of Wrath", "John Steinbeck"), True),
    (("Invisible Man", "Ralph Elison"), ("Invisible Man", "Ralph Ellison"), True),
    (("The Three Musketeers", "Alexander Dumas"), ("The Three Musketeers", "Alexandre Dumas"), True),
    (("The Art of War", "Sun Tsu"), ("The Art of War", "Sun Tzu"), True),
    (("The 48 Laws of Power", "Robert Green"), ("The 48 Laws of Power", "Robert Greene"), True),
    (("Cujo", "Steven King"), ("Cujo", "Stephen King"), True),
    # Cosmetic only: never suggested.
    (("The Hobbit", "J.R.R. Tolkien"), ("The Hobbit", "J. R. R. Tolkien"), False),
    (("the hobbit", "tolkien, j.r.r."), ("The Hobbit", "J.R.R. Tolkien"), False),
    (("Of Mice and Men", "John Steinbeck"), ("Of Mice & Men", "John Steinbeck"), False),
    (("One Hundred Years of Solitude", "Gabriel Garcia Marquez"),
     ("One Hundred Years of Solitude", "Gabriel García Márquez"), False),
    (("Catcher in the Rye", "J.D. Salinger"), ("The Catcher in the Rye", "J. D. Salinger"), False),
    (("Enders Game", "Orson Scott Card"), ("Ender's Game", "Orson Scott Card"), False),
    (("Moby Dick", "Herman Melville"), ("Moby-Dick", "Herman Melville"), False),
    (("Catch 22", "Joseph Heller"), ("Catch-22", "Joseph Heller"), False),
    (("Animal Farm", "Orwell"), ("Animal Farm", "George Orwell"), False),
    # A subtitle alone is not a correction.
    (("Man's Search for Meaning", "Viktor Frankl"),
     ("Man's Search for Meaning: An Introduction to Logotherapy", "Viktor Emil Frankl"), False),
    # Words added to the main title: other books.
    (("Search for Meaning", "Viktor Frankl"), ("Man's Search for Meaning", "Viktor E. Frankl"), False),
    (("Mans Search for Meaning", "Viktor Frankl"),
     ("Man's Search for Ultimate Meaning", "Viktor Emil Frankl"), False),
    (("Dune", "Frank Herbert"), ("Dune Messiah", "Frank Herbert"), False),
    (("Huckleberry Finn", "Mark Twain"), ("The Adventures of Huckleberry Finn", "Mark Twain"), False),
    # Different books, sequels, near-namesakes.
    (("To Kill a Mockingbird", "Harper Lee"), ("Go Set a Watchman", "Harper Lee"), False),
    (("Enders Game", "Orson Scott Card"), ("Ender's Shadow", "Orson Scott Card"), False),
    (("The Invisible Man", "Wells"), ("Invisible Man", "Ralph Ellison"), False),
    (("The Lord of the Rings", "Tolkien"), ("The Hobbit", "J.R.R. Tolkien"), False),
    # A number is never a typo.
    (("The 47 Laws of Power", "Robert Greene"), ("The 48 Laws of Power", "Robert Greene"), False),
    (("1948", "George Orwell"), ("1984", "George Orwell"), False),
    # Too far for a short word: "Flys" and "Carry" are two edits away.
    (("Lord of the Flys", "William Golding"), ("Lord of the Flies", "William Golding"), False),
    (("Carry", "Stephen King"), ("Carrie", "Stephen King"), False),
    # One edit in a 4-letter title is too much of it (ratio 0.75).
    (("Dume", "Frank Herbert"), ("Dune", "Frank Herbert"), False),
    # Another person of the same surname.
    (("Frankenstien", "Percy Shelley"), ("Frankenstein", "Mary Shelley"), False),
    # Invented by Claude: no candidate has it.
    (("Frankenstien", "Mary Shelley"), ("Frankenstein", "Percy Bysshe Shelley"), False),
    (("Belovd", "Toni Morrison"), ("Beloved: A Novel", "Toni Morrison"), False),
    # The typed spelling is itself in the catalog.
    (("The Grapes of Wroth", "John Steinbeck"), ("The Grapes of Wrath", "John Steinbeck"), False),
    # Two books score alike: "Ellis" is as near Wells as Ellison.
    (("Invisble Man", "Ellis"), ("Invisible Man", "Ralph Ellison"), False),
]  # fmt: skip
# pylint: enable=line-too-long


@pytest.mark.parametrize(("typed", "suggested", "expected"), EVALUATION)
def test_guard_evaluation(typed, suggested, expected):
    """The guard, given "same_book, high" from Claude, suggests only real fixes."""
    assert guard(typed, suggested) is expected


def test_evaluation_size():
    """The evaluation table covers both outcomes in some depth."""
    outcomes = [expected for _, _, expected in EVALUATION]
    assert len(outcomes) >= 40 and outcomes.count(True) >= 15


@pytest.mark.parametrize(
    ("verdict", "confidence"),
    [
        ("same_book", "medium"),
        ("same_book", "low"),
        ("different_book", "high"),
        ("ambiguous", "high"),
        ("no_match", "high"),
    ],
)
def test_guard_needs_same_book_high(verdict, confidence):
    """Anything but same_book with high confidence is silent."""
    typed, suggested = ("The Hobit", "J.R.R. Tolkien"), ("The Hobbit", "J.R.R. Tolkien")
    assert guard(typed, suggested)
    assert not guard(typed, suggested, verdict=verdict, confidence=confidence)


def test_guard_rejects_unknown_candidate_id():
    """A candidate id Claude made up is rejected."""
    groups = catalog_groups("The Hobit", "J.R.R. Tolkien")
    judgment = claude_says(groups, "The Hobbit", "J.R.R. Tolkien")
    judgment.candidate_id = "c99"
    assert not book_search.is_confident_suggestion(
        "The Hobit", "J.R.R. Tolkien", judgment, groups
    )


def test_guard_rejects_a_title_from_another_group():
    """The title must belong to the chosen candidate, not just any."""
    groups = catalog_groups("The Hobit", "J.R.R. Tolkien")
    judgment = claude_says(groups, "The Hobbit", "J.R.R. Tolkien")
    judgment.title = "The Annotated Hobbit"
    assert not book_search.is_confident_suggestion(
        "The Hobit", "J.R.R. Tolkien", judgment, groups
    )


def test_close_scores_are_silent():
    """Two different books scoring within the margin keep it back."""
    catalog = [("Belovd Ones", "Toni Morrison"), ("Beloved", "Toni Morrison")]
    catalog += [("Belovid", "Toni Morison")]
    groups = catalog_groups("Belovd", "Toni Morrison", catalog)
    judgment = claude_says(groups, "Beloved", "Toni Morrison")
    assert not book_search.is_confident_suggestion(
        "Belovd", "Toni Morrison", judgment, groups
    )
    alone = catalog_groups("Belovd", "Toni Morrison", catalog[1:2])
    assert book_search.is_confident_suggestion(
        "Belovd", "Toni Morrison", claude_says(alone, "Beloved", "Toni Morrison"), alone
    )


def test_groups_merge_editions():
    """Subtitles and format notes share a group; other authors don't."""
    groups = catalog_groups("The Hobbit", "Tolkien")
    hobbit = [g for g in groups if g.main == "hobbit"]
    assert [(g.surname, len(g.entries)) for g in hobbit] == [
        ("tolkien", 2),
        ("dixon", 1),
    ]
    assert [g.id for g in groups[:3]] == ["c1", "c2", "c3"]


def test_groups_json_for_claude():
    """Claude sees the typed text and compact groups, not the whole catalog."""
    groups = catalog_groups("The Hobit", "Tolkien")[:2]
    data = json.loads(book_search.groups_json("The Hobit", "Tolkien", groups))
    assert data["typed"] == {"title": "The Hobit", "author": "Tolkien"}
    assert data["candidates"][0]["id"] == "c1"
    assert data["candidates"][0]["titles"] == [
        "The Hobbit: or, There and Back Again",
        "The Hobbit",
    ]
    assert data["candidates"][0]["records"] == 2


def test_prompt_shares_the_formatting_rules():
    """Both Claude prompts use one copy of the title and author rules."""
    for prompt in (book_search.SYSTEM_PROMPT, books.SYSTEM_PROMPT):
        assert books.TITLE_RULE in prompt and books.AUTHOR_RULE in prompt


# --- Claude call ---------------------------------------------------------------


class FakeMessages:  # pylint: disable=too-few-public-methods
    """Stands in for ``AsyncAnthropic().messages``."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        """Record the call; return or raise the outcome."""
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def fake_anthropic(monkeypatch, outcome):
    """Replace the Anthropic client with one that answers ``outcome``."""
    messages = FakeMessages(outcome)

    class FakeClient:  # pylint: disable=too-few-public-methods
        """The client, with ``messages`` and ``close``."""

        def __init__(self, **_kwargs):
            self.messages = messages

        async def close(self):
            """Nothing to close."""

    monkeypatch.setattr(books.anthropic, "AsyncAnthropic", FakeClient)
    return messages


JUDGMENT = Judgment(
    verdict="same_book",
    candidate_id="c1",
    title="The Hobbit",
    author="J.R.R. Tolkien",
    confidence="high",
    notes="A one-letter typo.",
)


def run_judge(monkeypatch, outcome):
    """Call the real judge_with_claude against a fake client."""
    monkeypatch.undo()
    monkeypatch.setattr(
        books,
        "setting",
        lambda option, fallback="": "sk-test" if option == "anthropic_api_key" else "",
    )
    messages = fake_anthropic(monkeypatch, outcome)
    groups = catalog_groups("The Hobit", "Tolkien")[:2]
    return asyncio.run(book_search.judge_with_claude("The Hobit", "Tolkien", groups)), (
        messages
    )


def test_judge_with_claude(monkeypatch):
    """Structured output, the configured model, low effort, the shared prompt."""
    response = SimpleNamespace(parsed_output=JUDGMENT, stop_reason="end_turn")
    judgment, messages = run_judge(monkeypatch, response)
    assert judgment == JUDGMENT
    call = messages.calls[0]
    assert call["output_format"] is Judgment
    assert call["system"] == book_search.SYSTEM_PROMPT
    assert call["model"] == books.DEFAULT_MODEL
    assert call["output_config"] == {"effort": "low"}
    assert json.loads(call["messages"][0]["content"])["typed"]["title"] == "The Hobit"


@pytest.mark.parametrize(
    "outcome",
    [
        SimpleNamespace(parsed_output=None, stop_reason="refusal"),
        SimpleNamespace(parsed_output=JUDGMENT, stop_reason="refusal"),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
        anthropic.APIStatusError(
            "spend limit",
            response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
            body=None,
        ),
    ],
)
def test_judge_with_claude_failure(monkeypatch, outcome):
    """A refusal or an API error is None."""
    assert run_judge(monkeypatch, outcome)[0] is None


# --- suggest() -----------------------------------------------------------------

HOBBIT_FOUND = [("catalog", Candidate(title=t, author=a)) for t, a in CATALOG[:5]]


def stub_suggest(monkeypatch, found=HOBBIT_FOUND, judgment=JUDGMENT):
    """Stub the providers and Claude; count their calls."""
    calls = {"fetch": 0, "claude": 0}

    async def fetch(_title, _author):
        calls["fetch"] += 1
        return found

    async def judge(_title, _author, groups):
        calls["claude"] += 1
        assert len(groups) <= book_search.MAX_GROUPS
        return judgment

    monkeypatch.setattr(book_search, "fetch_search_candidates", fetch)
    monkeypatch.setattr(book_search, "judge_with_claude", judge)
    return calls


def suggest(title="The Hobit", author="J.R.R. Tolkien"):
    """Run book_search.suggest."""
    return asyncio.run(book_search.suggest(title, author))


def test_suggest(monkeypatch):
    """A confident correction is offered and cached."""
    calls = stub_suggest(monkeypatch)
    assert suggest() == book_search.Suggestion(
        title="The Hobbit",
        author="J.R.R. Tolkien",
        confidence="high",
        notes="A one-letter typo.",
    )
    assert suggest("the hobit", "Tolkien, J.R.R.") is not None
    assert calls == {"fetch": 1, "claude": 1}


def test_suggest_without_anthropic_key(monkeypatch, isolated):
    """No Anthropic key: no suggestion, and no provider is asked."""
    del isolated["anthropic_api_key"]
    stub_suggest(monkeypatch)
    monkeypatch.setattr(book_search, "fetch_search_candidates", None)
    assert suggest() is None


@pytest.fixture
def suggestions_off():
    """``[books] suggestions = false`` for one test."""
    config.set("books", "suggestions", "false")
    yield
    config.set("books", "suggestions", "true")


def test_suggestions_off(suggestions_off):  # pylint: disable=unused-argument
    """Switched off: None without any call (the autouse stubs would raise)."""
    assert not book_search.enabled()
    assert suggest() is None


def test_preset_skips_lookup():
    """IBP's own items are never looked up."""
    assert suggest("Resource Guide", "Inside Books Project") is None


def test_provider_error_not_cached(monkeypatch):
    """A provider failure is no suggestion, and the next request tries again."""
    calls = stub_suggest(monkeypatch, found=None)
    assert suggest() is None
    assert suggest() is None
    assert calls == {"fetch": 2, "claude": 0}


def test_claude_error_not_cached(monkeypatch):
    """A Claude failure is no suggestion, and the next request tries again."""
    calls = stub_suggest(monkeypatch, judgment=None)
    assert suggest() is None
    assert suggest() is None
    assert calls == {"fetch": 2, "claude": 2}


def test_no_possible_correction_skips_claude(monkeypatch):
    """When no candidate could pass the guard, Claude isn't asked; that's cached."""
    calls = stub_suggest(monkeypatch)
    assert suggest("The Hobbit", "J.R.R. Tolkien") is None
    assert suggest("The Hobbit", "J.R.R. Tolkien") is None
    assert calls == {"fetch": 1, "claude": 0}


def test_guard_rejection_is_cached(monkeypatch):
    """Claude's medium confidence: none, cached like any answer."""
    medium = JUDGMENT.model_copy(update={"confidence": "medium"})
    calls = stub_suggest(monkeypatch, judgment=medium)
    assert suggest() is None
    assert suggest() is None
    assert calls == {"fetch": 1, "claude": 1}


def test_cache_expiry(monkeypatch):
    """A suggestion lasts 30 days, none lasts 7."""
    now = [1000.0]
    monkeypatch.setattr(book_search, "clock", lambda: now[0])
    calls = stub_suggest(monkeypatch)
    day = 86400
    suggest()
    now[0] += 29 * day
    suggest()
    assert calls["fetch"] == 1
    now[0] += 2 * day
    suggest()
    assert calls["fetch"] == 2

    suggest("The Hobbit", "J.R.R. Tolkien")
    now[0] += 6 * day
    suggest("The Hobbit", "J.R.R. Tolkien")
    assert calls["fetch"] == 3
    now[0] += 2 * day
    suggest("The Hobbit", "J.R.R. Tolkien")
    assert calls["fetch"] == 4


def test_cache_size():
    """The oldest entry goes when the cache is full."""
    cache = book_search.SuggestionCache(size=2)
    for key in ("a", "b", "c"):
        cache.put((key, ""), None)
    assert list(cache.entries) == [("b", ""), ("c", "")]
    assert cache.get(("a", "")) == (False, None)
    assert cache.get(("b", "")) == (True, None)
