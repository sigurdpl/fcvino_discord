"""Circular initial avatars for the ten members.

Nobody has uploaded a photo and nobody is going to, so a member is drawn as
their initials on a coloured disc. The colour has to be *stable* — Tore being
teal one week and olive the next makes a page harder to read, not easier.

That means hashing with `hashlib` rather than the built-in `hash()`, which is
salted per process: `hash("Tore")` differs between runs, so every restart would
reshuffle the whole club. It is an easy thing to get wrong and an annoying thing
to notice later.

Pure: no database, no Jinja. `web/deps.py` exposes these to the templates.
"""

from __future__ import annotations

import hashlib

from bot.wine_origin import fold

# Picked to sit on both the light and the dark card, and to stay distinguishable
# from each other — ten members means ten discs on the same page.
PALETTE = (
    "#7a2e38",  # burgundy, the house colour
    "#2f5d62",  # teal
    "#6b4b8a",  # violet
    "#8a5a2b",  # amber
    "#2f6b4f",  # green
    "#8a3d5c",  # plum
    "#3a5a8a",  # blue
    "#7a6a2b",  # olive
    "#5a4a3a",  # brown
    "#2b6b7a",  # cyan
)


def initials(name: str | None, limit: int = 2) -> str:
    """'Håvard' -> 'H', 'Anne Mari' -> 'AM'. Folded, so Å survives as A."""
    if not name:
        return "?"
    words = fold(name).split()
    if not words:
        return "?"
    return "".join(word[0] for word in words[:limit]).upper()


def colour(name: str | None) -> str:
    """The same name always gets the same colour, in this run and the next.

    Folded first, so 'Tore' and 'tore' are one person and not two.
    """
    if not name:
        return PALETTE[0]
    digest = hashlib.sha256(fold(name).strip().encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]
