"""Comparing book titles typed or looked up by different people.

The same book's title can come back from the packing station, the portal or
a lookup with different case, accents, punctuation, "&" for "and", or its
article moved to the end ("The Hobbit", "Hobbit, The"). ``title_key`` drops
those differences, so two titles name the same book when their keys are
equal. Any other difference (a word changed, added or missing) still counts.
"""

import re
import unicodedata

ARTICLES = ("the", "a", "an")

# Apostrophes join a word ("Ender's" reads as "Enders"); any other
# punctuation separates words.
_APOSTROPHES = re.compile(r"['‘’ʼ`]")
_TRAILING_ARTICLE = re.compile(rf"^(.*\S)\s*,\s*(?:{'|'.join(ARTICLES)})$")
_NOT_WORD = re.compile(r"[\W_]+")


def title_key(title: str) -> str:
    """A title with case, accents, punctuation and its article ignored."""
    decomposed = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    text = " ".join(text.casefold().split())
    moved = _TRAILING_ARTICLE.match(text)
    if moved:
        text = moved.group(1)
    text = _APOSTROPHES.sub("", text.replace("&", " and "))
    words = _NOT_WORD.sub(" ", text).split()
    if len(words) > 1 and words[0] in ARTICLES:
        words = words[1:]
    return " ".join(words)
