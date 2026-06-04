import re

from modules.naming import ADJECTIVES, NOUNS, random_slug, unique_slug


def test_random_slug_is_adjective_noun():
    assert re.fullmatch(r"[a-z]+-[a-z]+", random_slug())


def test_slug_parts_come_from_the_word_lists():
    adj, noun = random_slug().split("-", 1)
    assert adj in ADJECTIVES
    assert noun in NOUNS


def test_unique_slug_never_repeats_within_a_batch():
    used = set()
    slugs = [unique_slug(used) for _ in range(40)]
    assert len(set(slugs)) == len(slugs)
    assert used == set(slugs)


def test_word_lists_are_non_trivial():
    # Enough combinations that a run's handful of clips won't collide.
    assert len(ADJECTIVES) >= 30 and len(NOUNS) >= 30
