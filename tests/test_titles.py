"""Title comparison: what counts as the same title."""

import pytest

from ibp.titles import title_key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("The Hobbit", "the hobbit"),
        ("The Hobbit", "Hobbit, The"),
        ("The Hobbit", "Hobbit"),
        ("A Tale of Two Cities", "Tale of Two Cities, A"),
        ("An Old Map", "Old Map, An"),
        ("War & Peace", "War and Peace"),
        ("Les Misérables", "Les Miserables"),
        ("Don Quixote!", "Don Quixote"),
        ("Dune: Messiah", "Dune Messiah"),
        ("Ender's Game", "Enders Game"),
        ("Ender’s Game", "Ender's Game"),
        ("  Moby-Dick  ", "moby dick"),
        ("Hobbit , The", "THE HOBBIT"),
    ],
)
def test_same_title(first, second):
    """Case, accents, punctuation, '&' and the article's place are ignored."""
    assert title_key(first) == title_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Dune", "Dune Messiah"),
        ("The Hobbit", "The Hobbits"),
        ("Emma", "Persuasion"),
        ("War and Peace", "War or Peace"),
        ("Of Mice and Men", "Mice and Men"),
        ("The Three Musketeers", "Three Musketeers, Four"),
    ],
)
def test_different_title(first, second):
    """A word changed, added or missing makes another title."""
    assert title_key(first) != title_key(second)


def test_a_lone_article_is_kept():
    """A title that is only an article still has a key."""
    assert title_key("The") == "the"
    assert title_key("A") != title_key("An")
