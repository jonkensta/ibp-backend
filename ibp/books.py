"""ISBN lookup: ask every book metadata provider, then merge them with Claude.

The providers (ISBNdb, Open Library, Google Books, Library of Congress) often
disagree on formatting and sometimes map an ISBN to the wrong book, so their
answers go to Claude, which picks one title and author. Without Claude (no
key configured, or the API failing, e.g. once the workspace spend limit is
reached) the first provider that answered wins, in the order of
``PROVIDERS``.

Keys come from the ``[books]`` config section; every key is optional (a
provider that needs one is skipped without it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

import anthropic
import httpx
from pydantic import BaseModel

from .base import config

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_TIMEOUT = 10.0

# The TDCJ Book Portal's title and author inputs have maxlength=100.
PORTAL_MAX_LENGTH = 100

USER_AGENT = "ibp-backend book lookup"


def setting(option: str, fallback: str = "") -> str:
    """Read a ``[books]`` option, stripped (empty if unset)."""
    return config.get("books", option, fallback=fallback).strip()


# --- ISBN normalization -----------------------------------------------------


def isbn13_check_digit(first12: str) -> str:
    """Check digit for the first 12 digits of an ISBN-13."""
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def isbn10_check_digit(first9: str) -> str:
    """Check digit for the first 9 digits of an ISBN-10."""
    total = sum((10 - i) * int(d) for i, d in enumerate(first9))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


# A leading label such as "ISBN", "ISBN-13:" or "isbn 10". The 10/13 only
# counts as part of the label when no digit follows it, so an ISBN-10 that
# starts with 10 or 13 ("ISBN 1305504917") keeps its digits.
_ISBN_LABEL = re.compile(r"^ISBN(?:[- ]?1[03](?!\d))?[ :]*", re.IGNORECASE)


def compact_isbn(raw: str) -> str:
    """Strip what normalize_isbn ignores, keeping the digits (and any X).

    Invisible format characters (left-to-right marks, zero-width spaces) are
    dropped, any dash reads as a hyphen and any space as a space; then a
    leading label goes, then the hyphens and spaces.
    """
    chars = []
    for ch in raw:
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category == "Pd":
            chars.append("-")
        elif ch.isspace() or category.startswith("Z"):
            chars.append(" ")
        else:
            chars.append(ch)
    text = _ISBN_LABEL.sub("", "".join(chars).strip())
    return re.sub(r"[ -]", "", text).upper()


def normalize_isbn(raw: str) -> Optional[str]:
    """Return the ISBN-13 for an ISBN-10, ISBN-13 or 18-digit EAN, or None.

    Spaces, dashes and invisible characters (such as the left-to-right marks
    a copy from a web page brings along) are ignored, as is a leading "ISBN",
    "ISBN-10" or "ISBN-13" label; an 18-digit scan (ISBN-13 plus a 5-digit
    price add-on) is trimmed. Bad check digits are rejected.
    """
    digits = compact_isbn(raw)
    if len(digits) == 18 and digits[:3] in ("978", "979"):
        digits = digits[:13]
    if re.fullmatch(r"\d{9}[\dX]", digits):
        if isbn10_check_digit(digits[:9]) != digits[9]:
            return None
        first12 = "978" + digits[:9]
        return first12 + isbn13_check_digit(first12)
    if re.fullmatch(r"97[89]\d{10}", digits):
        return digits if isbn13_check_digit(digits[:12]) == digits[12] else None
    return None


def isbn13_to_isbn10(isbn: str) -> Optional[str]:
    """ISBN-10 for a 978 ISBN-13 (979 ISBNs have none)."""
    if not isbn.startswith("978"):
        return None
    return isbn[3:12] + isbn10_check_digit(isbn[3:12])


# --- Providers --------------------------------------------------------------


@dataclass
class Candidate:
    """One provider's answer."""

    title: str
    author: str = ""
    publisher: str = ""
    date: str = ""


def join(*parts: Optional[str], sep: str = ": ") -> str:
    """Join the non-empty parts."""
    return sep.join(p.strip() for p in parts if p and p.strip())


def person_name(name: str) -> str:
    """'Last, First' -> 'First Last'; anything else unchanged."""
    parts = [p.strip() for p in str(name).split(",")]
    if len(parts) == 2 and all(parts):
        return f"{parts[1]} {parts[0]}"
    return str(name).strip()


Provider = Callable[[httpx.AsyncClient, str], Awaitable[Optional[Candidate]]]


async def isbndb(client: httpx.AsyncClient, isbn: str) -> Optional[Candidate]:
    """ISBNdb (needs ``isbndb_key``)."""
    key = setting("isbndb_key")
    if not key:
        return None
    response = await client.get(
        f"https://api2.isbndb.com/book/{isbn}", headers={"Authorization": key}
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    book = response.json().get("book") or {}
    if not book.get("title"):
        return None
    return Candidate(
        title=book.get("title_long") or book["title"],
        author=", ".join(person_name(a) for a in book.get("authors") or []),
        publisher=book.get("publisher") or "",
        date=str(book.get("date_published") or ""),
    )


async def open_library(client: httpx.AsyncClient, isbn: str) -> Optional[Candidate]:
    """Open Library (no key)."""
    response = await client.get(
        "https://openlibrary.org/api/books",
        params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
    )
    response.raise_for_status()
    book = response.json().get(f"ISBN:{isbn}") or {}
    if not book.get("title"):
        return None
    return Candidate(
        title=join(book.get("title"), book.get("subtitle")),
        author=", ".join(a.get("name", "") for a in book.get("authors") or []),
        publisher=", ".join(p.get("name", "") for p in book.get("publishers") or []),
        date=book.get("publish_date") or "",
    )


async def google_books(client: httpx.AsyncClient, isbn: str) -> Optional[Candidate]:
    """Google Books (``google_books_key`` optional, but keyless is throttled)."""
    params = {"q": f"isbn:{isbn}"}
    if key := setting("google_books_key"):
        params["key"] = key
    response = await client.get(
        "https://www.googleapis.com/books/v1/volumes", params=params
    )
    response.raise_for_status()
    items = response.json().get("items") or []
    info = items[0].get("volumeInfo") or {} if items else {}
    if not info.get("title"):
        return None
    return Candidate(
        title=join(info.get("title"), info.get("subtitle")),
        author=", ".join(info.get("authors") or []),
        publisher=info.get("publisher") or "",
        date=info.get("publishedDate") or "",
    )


LOC_SRU = "http://lx2.loc.gov:210/lcdb"  # no key; only served over http
MODS = "{http://www.loc.gov/mods/v3}"


def mods_name(name: ET.Element) -> str:
    """'Lee, Harper.' -> 'Lee, Harper', keeping an initial's period."""
    text = " ".join(
        p.text or "" for p in name.findall(f"{MODS}namePart") if not p.get("type")
    ).strip(" ,")
    return re.sub(r"(?<![A-Z])\.$", "", text)


def parse_loc_mods(xml: str) -> Optional[Candidate]:
    """Title, primary author, publisher and date from a MODS SRU response."""
    mods = ET.fromstring(xml).find(f".//{MODS}mods")
    if mods is None:
        return None
    title_info = next(
        (t for t in mods.findall(f"{MODS}titleInfo") if not t.get("type")), None
    )
    if title_info is None:
        return None

    def part(tag: str) -> str:
        return (title_info.findtext(f"{MODS}{tag}") or "").strip()

    title = join(join(part("nonSort"), part("title"), sep=" "), part("subTitle"))
    names = mods.findall(f"{MODS}name")
    authors = [n for n in names if n.get("usage") == "primary"] or [
        n
        for n in names
        if any(
            (r.text or "").strip(" .") == "author" for r in n.iter(f"{MODS}roleTerm")
        )
    ]
    author = ", ".join(person_name(mods_name(n)) for n in authors)
    origin = mods.find(f"{MODS}originInfo")
    publisher = date = ""
    if origin is not None:
        publisher = (
            origin.findtext(f"{MODS}agent/{MODS}namePart")
            or origin.findtext(f"{MODS}publisher")
            or ""
        )
        date = origin.findtext(f"{MODS}dateIssued") or ""
    return Candidate(
        title=title.strip(" /:;,."), author=author, publisher=publisher, date=date
    )


async def library_of_congress(
    client: httpx.AsyncClient, isbn: str
) -> Optional[Candidate]:
    """Library of Congress catalog (no key). Older records index only the ISBN-10."""
    for query in filter(None, (isbn, isbn13_to_isbn10(isbn))):
        response = await client.get(
            LOC_SRU,
            params={
                "version": "1.1",
                "operation": "searchRetrieve",
                "query": f"bath.isbn={query}",
                "maximumRecords": "1",
                "recordSchema": "mods",
            },
        )
        response.raise_for_status()
        if "<mods" in response.text:
            return parse_loc_mods(response.text)
    return None


# Fallback order when Claude can't merge: the extension's historical order.
PROVIDERS: dict[str, Provider] = {
    "isbndb": isbndb,
    "openlibrary": open_library,
    "google": google_books,
    "loc": library_of_congress,
}


def redact(message: str) -> str:
    """Hide API keys that httpx puts in error messages via the URL."""
    return re.sub(r"key=[^&\s]+", "key=***", message)


async def fetch_candidates(isbn: str) -> dict[str, Candidate]:
    """Ask every provider at once; return the ones that know the ISBN."""
    timeout = config.getfloat("books", "timeout", fallback=DEFAULT_TIMEOUT)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as client:

        async def ask(name: str, provider: Provider) -> Optional[Candidate]:
            try:
                return await provider(client, isbn)
            except (httpx.HTTPError, ValueError, ET.ParseError) as error:
                logger.warning(
                    "%s lookup of %s failed: %s", name, isbn, redact(str(error))
                )
                return None

        results = await asyncio.gather(
            *(ask(name, provider) for name, provider in PROVIDERS.items())
        )
    return {
        name: candidate
        for name, candidate in zip(PROVIDERS, results)
        if candidate is not None
    }


# --- Claude merge -----------------------------------------------------------

SYSTEM_PROMPT = f"""\
You merge book metadata from several ISBN lookup services into the single \
best title and author for a packing slip and a prison book-portal form.

Rules:
- Title: the book's real title as printed on the cover, with a subtitle only \
if it is part of how the book is known. Drop edition notes, series numbering \
in brackets, format words (paperback, large print) and trailing junk. Use \
normal title case. At most {PORTAL_MAX_LENGTH} characters.
- Author: the primary author(s) as "First Last", comma-separated, at most \
{PORTAL_MAX_LENGTH} characters. Leave out editors, translators and \
illustrators unless there is no author. Empty if no source names one.
- Prefer what most sources agree on. If one source is clearly a different \
book (wrong ISBN mapping), ignore it and say so in notes.
- confidence: high if the sources agree once formatting is set aside, medium \
if you had to choose between plausible variants, low if the sources describe \
different books or the data is too thin to trust.
- Never invent a title or author that no source supports."""


class Merged(BaseModel):
    """Claude's merged answer."""

    title: str
    author: str
    confidence: Literal["high", "medium", "low"]
    notes: str


def candidates_json(isbn: str, candidates: dict[str, Candidate]) -> str:
    """The candidates as the JSON Claude sees (and the cache stores)."""
    return json.dumps(
        {"isbn": isbn, "sources": {k: vars(v) for k, v in candidates.items()}},
        ensure_ascii=False,
        indent=1,
    )


async def merge_with_claude(
    isbn: str, candidates: dict[str, Candidate]
) -> Optional[Merged]:
    """Claude's merge of the candidates, or None if it isn't available."""
    api_key = setting("anthropic_api_key")
    if not api_key:
        return None
    model = setting("model") or DEFAULT_MODEL
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1)
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=2048,
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": candidates_json(isbn, candidates)}],
            output_format=Merged,
        )
    except anthropic.APIStatusError as error:
        logger.warning("Claude merge of %s failed: HTTP %s", isbn, error.status_code)
        return None
    except anthropic.APIConnectionError as error:
        logger.warning("Claude merge of %s failed: %s", isbn, error)
        return None
    finally:
        await client.close()
    merged = response.parsed_output
    if response.stop_reason == "refusal" or merged is None or not merged.title.strip():
        logger.warning(
            "Claude merge of %s gave no answer (stop_reason=%s)",
            isbn,
            response.stop_reason,
        )
        return None
    logger.info(
        "Claude merged %s (%s confidence): %s", isbn, merged.confidence, merged.notes
    )
    return merged


# --- Lookup -----------------------------------------------------------------


@dataclass
class BookResult:
    """The answer for one ISBN.

    ``cacheable`` is False for a provider fallback taken because Claude was
    unavailable, so a later lookup gets another chance at a merge.
    """

    title: str
    author: Optional[str]
    source: str
    confidence: Optional[str]
    candidates: str
    cacheable: bool


def clip(text: str) -> str:
    """Collapse whitespace and drop control characters; cap at the portal limit."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:PORTAL_MAX_LENGTH].strip()


async def lookup(isbn: str) -> Optional[BookResult]:
    """Look up an ISBN-13; None if no provider knows it."""
    candidates = await fetch_candidates(isbn)
    if not candidates:
        return None
    raw = candidates_json(isbn, candidates)

    merged = await merge_with_claude(isbn, candidates)
    if merged is not None:
        return BookResult(
            title=clip(merged.title),
            author=clip(merged.author) or None,
            source="claude",
            confidence=merged.confidence,
            candidates=raw,
            cacheable=True,
        )

    name, first = next(iter(candidates.items()))
    return BookResult(
        title=clip(first.title),
        author=clip(first.author) or None,
        source=name,
        confidence=None,
        candidates=raw,
        # With Claude configured, a fallback is a stopgap worth retrying.
        cacheable=not setting("anthropic_api_key"),
    )
