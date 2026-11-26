"""Title and author suggestions for a book a volunteer typed by hand.

The typed title and author are searched for at the book providers (ISBNdb,
Google Books, Open Library, Library of Congress). The results are grouped by
book, ranked against what was typed, and the closest few go to Claude, which
says whether one of them is the same book spelled better. A suggestion is
offered only when Claude is sure *and* the deterministic guard
(``is_confident_suggestion``) agrees: a wrong suggestion that a tired
volunteer accepts is worse than none, since these titles end up on the TDCJ
Book Portal.

Without an Anthropic key there are no suggestions. Nothing is stored except
in a small in-process cache; no ISBN is ever suggested.
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import json
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Literal, Optional

import httpx
from pydantic import BaseModel

from . import books
from .base import config
from .books import Candidate, setting

logger = logging.getLogger(__name__)

# IBP's own items with no ISBN skip the lookup. Keep in step with PRESETS
# near the top of book-portal-extension/content.js.
PRESET_TITLES = ("Resource Guide", "Self-Care on the Inside")

# Results asked of each provider query.
SEARCH_RESULTS = 10
GOOGLE_RESULTS = 20
# Candidate books (groups) shown to Claude.
MAX_GROUPS = 8
# Weight of the main title (vs the author surname) in a group's score.
TITLE_WEIGHT = 0.8
# Guard thresholds (see is_confident_suggestion). The main-title difflib
# ratio is 0.80 rather than 0.85 so that a swapped pair of letters in a
# 5-letter title ("Nigth") can still be fixed.
MIN_TITLE_RATIO = 0.80
MAX_SURNAME_DISTANCE = 2
UNIQUE_MARGIN = 0.05
MAX_NOTES_LENGTH = 300
# Shortest title or author the portal takes (schemas.PORTAL_MIN_LENGTH).
MIN_LENGTH = 3

CACHE_SIZE = 1000
SUGGESTION_TTL = datetime.timedelta(days=30).total_seconds()
MISS_TTL = datetime.timedelta(days=7).total_seconds()


def enabled() -> bool:
    """Whether ``[books] suggestions`` is on (the default)."""
    return config.getboolean("books", "suggestions", fallback=True)


# --- Normalization (for comparison only) -------------------------------------
#
# Self-contained on purpose; package_api has its own title normalizer for
# portal matching, and the two should be unified.

_ARTICLES = ("the", "a", "an")
# Words that are not part of a name ("Strunk and White", "King, Jr.").
_NOT_NAMES = frozenset({"and", "jr", "sr", "ii", "iii", "iv"})
# A subtitle starts at a colon or semicolon, or at the ", or," of
# "The hobbit, or, There and back again".
_SUBTITLE = re.compile(r"[:;]|,\s+or,\s", re.IGNORECASE)
# A trailing note such as "(Paperback)" or "[adaptation]".
_TRAILING_NOTE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")
_AUTHOR_SEPARATOR = re.compile(r",|;|&|\band\b", re.IGNORECASE)


def fold(text: str) -> str:
    """Casefold, strip diacritics, '&' -> 'and', punctuation out, spaces collapsed.

    Apostrophes are dropped ("Man's" -> "mans"); other punctuation separates
    words.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"['‘’ʼ`]", "", text.replace("&", " and "))
    return " ".join(re.sub(r"[^\w\s]|_", " ", text).split())


def normalize_title(title: str) -> str:
    """A title for comparison: folded, a leading or ", The" article dropped."""
    title = re.sub(r",\s*(the|a|an)\s*$", "", title.strip(), flags=re.IGNORECASE)
    words = fold(title).split()
    if len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def raw_main_title(title: str) -> str:
    """The part of ``title`` before any subtitle, without trailing notes."""
    main = _SUBTITLE.split(title, maxsplit=1)[0]
    while (stripped := _TRAILING_NOTE.sub("", main)) != main:
        main = stripped
    return main.strip()


def normalize_main_title(title: str) -> str:
    """The normalized main title (or whole title, if that leaves nothing)."""
    return normalize_title(raw_main_title(title)) or normalize_title(title)


def name_words(name: str) -> list[str]:
    """Folded words of a name, without initials, suffixes or parentheses."""
    name = re.sub(r"\([^)]*\)", " ", name).replace(".", " ")
    return [w for w in fold(name).split() if len(w) > 1 and w not in _NOT_NAMES]


def normalize_author(author: str) -> str:
    """An author for comparison: "Last, First" -> "first last", initials dropped."""
    parts = [p.strip() for p in author.split(",") if p.strip()]
    # "Hurston, Zora" turns round; "Zora Neale Hurston, Henry Louis Gates"
    # (two authors) doesn't, nor "King, Jr.".
    if (
        len(parts) == 2
        and len(parts[0].split()) <= 2
        and fold(parts[1].replace(".", " ")) not in _NOT_NAMES
    ):
        parts = [parts[1], parts[0]]
    return " ".join(name_words(" ".join(parts)))


def surname(author: str) -> str:
    """The first author's surname, folded ("" if none)."""
    words = name_words(_AUTHOR_SEPARATOR.split(author, maxsplit=1)[0])
    return words[-1] if words else ""


def authors_match(first: str, second: str) -> bool:
    """Same surname, and one name's words are a subset of the other's."""
    if not surname(first) or surname(first) != surname(second):
        return False
    a, b = set(normalize_author(first).split()), set(normalize_author(second).split())
    return a <= b or b <= a


def edit_distance(a: str, b: str) -> int:
    """Edits (insert, delete, substitute, swap neighbours) turning a into b."""
    rows = [list(range(len(b) + 1))]
    for i, char_a in enumerate(a, 1):
        row = [i]
        for j, char_b in enumerate(b, 1):
            cost = int(char_a != char_b)
            best = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + cost)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                best = min(best, rows[-2][j - 2] + 1)
            row.append(best)
        rows.append(row)
    return rows[-1][-1]


def ratio(a: str, b: str) -> float:
    """difflib similarity of two normalized strings (0 if either is empty)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def word_limit(word: str) -> int:
    """Edits allowed in a typed word: 1 up to 5 characters, else 2.

    None in a word with a digit: "The 33 Strategies of War" is not a typo of
    "The 48 Laws of Power", nor volume 2 of volume 3.
    """
    if any(c.isdigit() for c in word):
        return 0
    return 1 if len(word) <= 5 else 2


def is_preset(title: str) -> bool:
    """Whether ``title`` is one of IBP's own items (PRESET_TITLES)."""
    return normalize_main_title(title) in {
        normalize_main_title(p) for p in PRESET_TITLES
    }


# --- Providers ----------------------------------------------------------------

SearchProvider = Callable[[httpx.AsyncClient, str, str], Awaitable[list[Candidate]]]


async def isbndb_search(
    client: httpx.AsyncClient, title: str, author: str
) -> list[Candidate]:
    """ISBNdb field search (needs ``isbndb_key``)."""
    key = setting("isbndb_key")
    if not key:
        return []
    response = await client.get(
        "https://api2.isbndb.com/search/books",
        params={"text": title, "author": author, "pageSize": str(SEARCH_RESULTS)},
        headers={"Authorization": key},
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    found = (books.isbndb_candidate(b) for b in response.json().get("data") or [])
    return [c for c in found if c is not None]


async def google_search(
    client: httpx.AsyncClient, title: str, author: str
) -> list[Candidate]:
    """Google Books (needs ``google_books_key``: keyless, the quota runs out)."""
    key = setting("google_books_key")
    if not key:
        return []
    response = await client.get(
        "https://www.googleapis.com/books/v1/volumes",
        params={
            "q": f"intitle:{title} inauthor:{author}",
            "maxResults": str(GOOGLE_RESULTS),
            "printType": "books",
            "key": key,
        },
    )
    response.raise_for_status()
    found = (
        books.google_candidate(item.get("volumeInfo") or {})
        for item in response.json().get("items") or []
    )
    return [c for c in found if c is not None]


OPEN_LIBRARY_FIELDS = "title,subtitle,author_name,publisher,first_publish_year"


def parse_open_library(data: dict) -> list[Candidate]:
    """Candidates from an Open Library ``search.json`` response."""
    candidates = []
    for doc in data.get("docs") or []:
        if not doc.get("title"):
            continue
        candidates.append(
            Candidate(
                title=books.join(doc.get("title"), doc.get("subtitle")),
                author=", ".join(doc.get("author_name") or []),
                publisher=next(iter(doc.get("publisher") or []), ""),
                date=str(doc.get("first_publish_year") or ""),
            )
        )
    return candidates


async def search_open_library(
    client: httpx.AsyncClient, params: dict[str, str]
) -> list[Candidate]:
    """One Open Library search."""
    response = await client.get(
        "https://openlibrary.org/search.json",
        params={**params, "fields": OPEN_LIBRARY_FIELDS, "limit": str(SEARCH_RESULTS)},
    )
    response.raise_for_status()
    return parse_open_library(response.json())


async def open_library_search(
    client: httpx.AsyncClient, title: str, author: str
) -> list[Candidate]:
    """Open Library by title and author (no key; no typo tolerance)."""
    return await search_open_library(client, {"title": title, "author": author})


async def open_library_title_search(
    client: httpx.AsyncClient, title: str, _author: str
) -> list[Candidate]:
    """Open Library by title alone, which still finds a misspelled author."""
    return await search_open_library(client, {"title": title})


def cql_phrase(text: str) -> str:
    """A quoted CQL term, without the characters CQL treats specially."""
    return '"' + " ".join(re.sub(r'["\\*?^]', " ", text).split()) + '"'


def parse_loc_records(xml: str) -> list[Candidate]:
    """Candidates from every record of a MODS SRU response."""
    found = (
        books.mods_candidate(mods)
        for mods in ET.fromstring(xml).iter(f"{books.MODS}mods")
    )
    return [c for c in found if c is not None and c.title]


async def library_of_congress_search(
    client: httpx.AsyncClient, title: str, author: str
) -> list[Candidate]:
    """Library of Congress catalog by title and creator words (no key)."""
    response = await client.get(
        books.LOC_SRU,
        params={
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": f"dc.title={cql_phrase(title)} and "
            f"dc.creator={cql_phrase(author)}",
            "maximumRecords": str(SEARCH_RESULTS),
            "recordSchema": "mods",
        },
    )
    response.raise_for_status()
    return parse_loc_records(response.text)


SEARCH_PROVIDERS: dict[str, SearchProvider] = {
    "isbndb": isbndb_search,
    "google": google_search,
    "openlibrary": open_library_search,
    "openlibrary_title": open_library_title_search,
    "loc": library_of_congress_search,
}


async def fetch_search_candidates(
    title: str, author: str
) -> Optional[list[tuple[str, Candidate]]]:
    """Ask every provider at once: (provider, candidate) pairs.

    None if any provider failed, since a missing provider might have held the
    competing book that should have kept the suggestion back.
    """
    timeout = config.getfloat("books", "timeout", fallback=books.DEFAULT_TIMEOUT)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": books.USER_AGENT}
    ) as client:

        async def ask(name: str, provider: SearchProvider) -> Optional[list]:
            try:
                return await provider(client, title, author)
            except (httpx.HTTPError, ValueError, ET.ParseError) as error:
                logger.warning(
                    "%s title search failed: %s", name, books.redact(str(error))
                )
                return None

        results = await asyncio.gather(
            *(ask(name, provider) for name, provider in SEARCH_PROVIDERS.items())
        )
    if any(found is None for found in results):
        return None
    return [
        (name, candidate)
        for name, found in zip(SEARCH_PROVIDERS, results)
        for candidate in found or []
    ]


# --- Grouping and ranking -------------------------------------------------------


@dataclass
class Group:
    """Candidates for one book: the same main title and author surname."""

    main: str
    surname: str
    entries: list[tuple[str, Candidate]] = field(default_factory=list)
    id: str = ""
    score: float = 0.0


def group_candidates(
    title: str, author: str, found: list[tuple[str, Candidate]]
) -> list[Group]:
    """Group candidates by book and rank the groups against the typed text.

    Best first, with ids ``c1``, ``c2``, ... in that order.
    """
    groups: dict[tuple[str, str], Group] = {}
    for source, candidate in found:
        key = (normalize_main_title(candidate.title), surname(candidate.author))
        if not key[0]:
            continue
        groups.setdefault(key, Group(*key)).entries.append((source, candidate))
    typed_main, typed_surname = normalize_main_title(title), surname(author)
    for group in groups.values():
        group.score = TITLE_WEIGHT * ratio(typed_main, group.main) + (
            1 - TITLE_WEIGHT
        ) * ratio(typed_surname, group.surname)
    ranked = sorted(groups.values(), key=lambda g: g.score, reverse=True)
    for index, group in enumerate(ranked, 1):
        group.id = f"c{index}"
    return ranked


def distinct(values: Iterable[str], limit: int) -> list[str]:
    """The first ``limit`` distinct non-empty values, in order."""
    return list(dict.fromkeys(v for v in values if v))[:limit]


def groups_json(title: str, author: str, groups: list[Group]) -> str:
    """What Claude sees: the typed text and the candidate books."""
    return json.dumps(
        {
            "typed": {"title": title, "author": author},
            "candidates": [
                {
                    "id": g.id,
                    "titles": distinct((c.title for _, c in g.entries), 5),
                    "authors": distinct((c.author for _, c in g.entries), 3),
                    "publishers": distinct((c.publisher for _, c in g.entries), 3),
                    "years": distinct(
                        sorted(c.date[:4] for _, c in g.entries if c.date), 5
                    ),
                    "sources": distinct((s for s, _ in g.entries), 5),
                    "records": len(g.entries),
                }
                for g in groups
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


# --- Claude judgment ------------------------------------------------------------

SYSTEM_PROMPT = f"""\
A volunteer at a books-to-prisoners program typed a book's title and author \
by hand. You get what they typed and candidate books found by searching book \
catalogs for it. Say whether one candidate is the same book the volunteer \
meant, spelled better. A wrong correction that a tired volunteer accepts is \
worse than none, so when in doubt, do not choose.

verdict:
- same_book: one candidate is the book the volunteer meant, and what they \
typed differs from it only by typos, punctuation, capitalization, a missing \
or extra subtitle, or how the author's name is written.
- different_book: the close candidates are other books: a sequel, an \
adaptation, a companion, study guide or summary, a different book with a \
similar title, or the same title by a different author.
- ambiguous: more than one candidate could be what the volunteer meant.
- no_match: no candidate is close.

For same_book, candidate_id is the chosen candidate's id, and title and \
author are that book's, written by these rules:
{books.TITLE_RULE}
{books.AUTHOR_RULE}
- Take the title and author only from the chosen candidate's titles and \
authors; never add a word to the main title that no candidate has.
For any other verdict, candidate_id, title and author are empty.

confidence: high only if you are sure it is the same work (not an \
adaptation, abridgment, companion or sequel) and no other candidate is \
plausible; medium if probably; low otherwise.
notes: one short sentence on why."""


class Judgment(BaseModel):
    """Claude's answer about the typed book."""

    verdict: Literal["same_book", "different_book", "ambiguous", "no_match"]
    candidate_id: str
    title: str
    author: str
    confidence: Literal["high", "medium", "low"]
    notes: str


async def judge_with_claude(
    title: str, author: str, groups: list[Group]
) -> Optional[Judgment]:
    """Claude's judgment of the candidate groups, or None if the call failed.

    The same call as the ISBN merge (``books.ask_claude``): model, effort,
    retry and refusal handling.
    """
    return await books.ask_claude(
        "book suggestion", SYSTEM_PROMPT, groups_json(title, author, groups), Judgment
    )


# --- Guard --------------------------------------------------------------------


def supported(title: str, author: str, group: Group) -> bool:
    """Whether a candidate of ``group`` has this title and author.

    The title must equal a candidate's title or main title; the author must
    share a candidate's surname and use only words of that candidate's name
    (dropping a middle name is fine, adding one is not).
    """
    norm = normalize_title(title)
    if not any(
        norm in (normalize_title(c.title), normalize_main_title(c.title))
        for _, c in group.entries
    ):
        return False
    words = set(normalize_author(author).split())
    return any(
        surname(author) == surname(c.author)
        and words <= set(normalize_author(c.author).split())
        for _, c in group.entries
    )


def bounded_title_edit(typed: str, suggested: str) -> bool:
    """A small edit between normalized main titles, adding no word.

    Word for word, each typed word within ``word_limit`` edits of the
    suggested one, and the whole within MIN_TITLE_RATIO.
    """
    typed_words, suggested_words = typed.split(), suggested.split()
    if not typed_words or len(typed_words) != len(suggested_words):
        return False
    if any(
        edit_distance(t, s) > word_limit(t)
        for t, s in zip(typed_words, suggested_words)
    ):
        return False
    return ratio(typed, suggested) >= MIN_TITLE_RATIO


def bounded_author_edit(typed: str, suggested: str, group: Group) -> bool:
    """The surname within MAX_SURNAME_DISTANCE, and no other person's names.

    Each typed given name must be close to a given name the group's
    candidates or the suggestion use for this surname, so "Mary Shelley"
    never becomes "Percy Bysshe Shelley". Skipped if none gives one.
    """
    last = surname(suggested)
    if not last or edit_distance(surname(typed), last) > MAX_SURNAME_DISTANCE:
        return False
    known = {w for w in normalize_author(suggested).split() if w != last}
    for _, candidate in group.entries:
        if surname(candidate.author) == last:
            known |= set(normalize_author(candidate.author).split()) - {last}
    if not known:
        return True
    given = [w for w in normalize_author(typed).split() if w != surname(typed)]
    return all(any(edit_distance(w, k) <= word_limit(w) for k in known) for w in given)


def is_confident_suggestion(
    title: str, author: str, judgment: Judgment, groups: list[Group]
) -> bool:
    """Whether Claude's judgment may be offered; all of these must hold.

    a. Claude said same_book with high confidence, about one of ``groups``,
       and its title and author are that group's (nothing invented).
    b. It is more than cosmetic: the main title or the author differs from
       what was typed after normalization (so case, punctuation, articles,
       name order, initials or a subtitle alone are never suggested).
    c. The edit is small: word for word with no word added to the main title
       (``bounded_title_edit``), and the surname close (``bounded_author_edit``).
    d. It is unique (``unique``).
    """
    chosen = next((g for g in groups if g.id == judgment.candidate_id), None)
    if judgment.verdict != "same_book" or judgment.confidence != "high" or not chosen:
        return False
    suggested_title, suggested_author = clean(judgment.title), clean(judgment.author)
    typed_main = normalize_main_title(title)
    suggested_main = normalize_main_title(suggested_title)
    return (
        min(len(suggested_title), len(suggested_author)) >= MIN_LENGTH
        and supported(suggested_title, suggested_author, chosen)
        and not (
            typed_main == suggested_main and authors_match(author, suggested_author)
        )
        and bounded_title_edit(typed_main, suggested_main)
        and bounded_author_edit(author, suggested_author, chosen)
        and unique(title, author, chosen, groups)
    )


def unique(title: str, author: str, chosen: Group, groups: list[Group]) -> bool:
    """Whether ``chosen`` stands alone among ``groups``.

    What was typed must not itself be a listed book (a catalog can hold the
    typed spelling), and no other group may score within UNIQUE_MARGIN of
    the chosen one. Editions that differ only in subtitle or a format note
    are in the chosen group already.
    """
    typed_main = normalize_main_title(title)
    if any(
        g.main == typed_main
        and any(authors_match(author, c.author) for _, c in g.entries)
        for g in groups
    ):
        return False
    return not any(
        g is not chosen and g.score >= chosen.score - UNIQUE_MARGIN for g in groups
    )


def clean(text: str) -> str:
    """Whitespace collapsed, control characters out, capped at the portal limit."""
    return books.clip(text)


def worth_asking(title: str, author: str, groups: list[Group]) -> bool:
    """Whether any candidate, taken as Claude's answer, would pass the guard.

    If none would, Claude is not asked.
    """
    for group in groups[:MAX_GROUPS]:
        for _, candidate in group.entries:
            options = (candidate.title, raw_main_title(candidate.title))
            for option in dict.fromkeys(options):
                judgment = Judgment(
                    verdict="same_book",
                    candidate_id=group.id,
                    title=option,
                    author=candidate.author,
                    confidence="high",
                    notes="",
                )
                if is_confident_suggestion(title, author, judgment, groups):
                    return True
    return False


# --- Suggestion and cache -------------------------------------------------------


@dataclass
class Suggestion:
    """A corrected title and author to offer the volunteer."""

    title: str
    author: str
    confidence: str
    notes: str


def clock() -> float:
    """Seconds on a monotonic clock (patched in tests)."""
    return time.monotonic()


class SuggestionCache:
    """An in-process LRU cache of suggestions (or their absence) with a TTL."""

    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self.entries: OrderedDict[tuple[str, str], tuple[float, Optional[Suggestion]]]
        self.entries = OrderedDict()

    def get(self, key: tuple[str, str]) -> tuple[bool, Optional[Suggestion]]:
        """(hit, suggestion) for ``key``; an expired entry is a miss."""
        entry = self.entries.get(key)
        if entry is None or entry[0] <= clock():
            self.entries.pop(key, None)
            return False, None
        self.entries.move_to_end(key)
        return True, entry[1]

    def put(self, key: tuple[str, str], suggestion: Optional[Suggestion]) -> None:
        """Remember ``suggestion`` for SUGGESTION_TTL, or its absence for MISS_TTL."""
        ttl = SUGGESTION_TTL if suggestion is not None else MISS_TTL
        self.entries[key] = (clock() + ttl, suggestion)
        self.entries.move_to_end(key)
        while len(self.entries) > self.size:
            self.entries.popitem(last=False)

    def clear(self) -> None:
        """Forget everything."""
        self.entries.clear()


CACHE = SuggestionCache()


async def suggest(title: str, author: str) -> Optional[Suggestion]:
    """A better spelling of the typed title and author, or None.

    None when suggestions are off, for IBP's own items, without an Anthropic
    key, when a provider or Claude fails (not cached), or when the guard
    finds no confident suggestion (cached, as is a suggestion).
    """
    if not enabled() or is_preset(title) or not setting("anthropic_api_key"):
        return None
    key = (normalize_title(title), normalize_author(author))
    hit, cached = CACHE.get(key)
    if hit:
        return cached

    found = await fetch_search_candidates(title, author)
    if found is None:
        return None
    groups = group_candidates(title, author, found)
    if not worth_asking(title, author, groups):
        logger.info(
            "Book suggestion: none (%d candidates in %d groups; Claude not asked)",
            len(found),
            len(groups),
        )
        CACHE.put(key, None)
        return None

    judgment = await judge_with_claude(title, author, groups[:MAX_GROUPS])
    if judgment is None:
        return None
    suggestion = None
    if is_confident_suggestion(title, author, judgment, groups):
        suggestion = Suggestion(
            title=clean(judgment.title),
            author=clean(judgment.author),
            confidence=judgment.confidence,
            notes=" ".join(judgment.notes.split())[:MAX_NOTES_LENGTH],
        )
    logger.info(
        "Book suggestion: %s (verdict %s, %s confidence; %d candidates in %d groups)",
        "offered" if suggestion else "none",
        judgment.verdict,
        judgment.confidence,
        len(found),
        len(groups),
    )
    CACHE.put(key, suggestion)
    return suggestion
