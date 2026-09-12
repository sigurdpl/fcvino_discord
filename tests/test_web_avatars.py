"""Initial avatars for the members."""

from __future__ import annotations

import subprocess
import sys

import pytest

from web.avatars import PALETTE, colour, initials

MEMBERS = ("Andy", "Erk", "Håvard", "Lennart", "Marius",
           "Morten", "Robert", "Sigurd", "Thomas", "Tore")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Tore", "T"),
        ("Håvard", "H"),        # folded, so Å becomes A rather than vanishing
        ("Øystein", "O"),
        ("Anne Mari", "AM"),
        ("jan erik olsen", "JE"),
    ],
)
def test_initials(name, expected):
    assert initials(name) == expected


@pytest.mark.parametrize("name", [None, "", "   ", "!!!"])
def test_a_nameless_member_still_gets_a_disc(name):
    assert initials(name) == "?"
    assert colour(name) in PALETTE


def test_a_colour_is_always_from_the_palette():
    assert all(colour(name) in PALETTE for name in MEMBERS)


def test_the_same_name_always_gets_the_same_colour():
    assert colour("Tore") == colour("Tore")


def test_case_and_accents_do_not_split_a_person():
    assert colour("Tore") == colour("tore") == colour("TORE")
    assert colour("Håvard") == colour("havard")


def test_colours_are_stable_across_processes():
    """The reason this uses hashlib and not the built-in hash().

    `hash()` on a str is salted per process, so with it every restart would deal
    the club a fresh set of colours. Two interpreters must agree.
    """
    code = (
        "from web.avatars import colour;"
        "print(','.join(colour(n) for n in "
        f"{MEMBERS!r}))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1, "the same names produced different colours in two processes"
    assert runs.pop() == ",".join(colour(n) for n in MEMBERS)


def test_the_club_is_reasonably_spread_over_the_palette():
    """Not a guarantee — ten hashes into ten buckets will collide — but all ten
    landing on two colours would make the avatars useless, and is worth knowing."""
    assert len({colour(n) for n in MEMBERS}) >= 5
