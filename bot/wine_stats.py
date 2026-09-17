"""Rolling the cellar up by country, region and grape.

Pure functions over rows — no database, no Discord — so the arithmetic can be
tested directly. `bot/cogs/wine.py` does the SQL and the rendering.

Two decisions worth stating, because both change what the boards say:

**An average is the mean of every individual rating, not the mean of the wines'
averages.** A bottle nine of us scored therefore counts for more than one only
three people were around for, which is what "how does the club rate Barolo"
actually means.

**A country or grape needs MIN_WINES bottles to appear.** Otherwise the top of
the table is whichever region we happened to drink one good bottle from, and the
board says nothing about the club's taste.

A bucket also counts the *evenings* behind it, because five bottles opened on one
night are five opinions about that night. The five Dolcettos in the cellar all
come from a single tasting and average 57 — comfortably last. That may well be a
verdict on Dolcetto, but the data cannot tell it apart from one bad evening, so
the board marks it rather than pretending otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import NamedTuple, Protocol

MIN_WINES = 5

# How many scores a single bottle needs before it can be ranked against the
# others. Lives here rather than in the Discord cog that used to own it, because
# the web app asks the same question and must not answer it differently.
MIN_RATINGS = 2


class Row(Protocol):
    """What `tally` needs: a bucket, the bottle and evening it came from, one score."""

    def __getitem__(self, key: str): ...


class Tally(NamedTuple):
    name: str
    wines: int
    ratings: int
    tastings: int
    average: float

    @property
    def one_evening(self) -> bool:
        """Every bottle poured on the same night — a verdict on that night as much
        as on the grape."""
        return self.tastings == 1


def tally(rows: Iterable[Row], *, minimum: int = MIN_WINES) -> list[Tally]:
    """Group rows by `key`, best average first.

    Each row is one rating: `key` (the country, grape, …), `wine_id`,
    `tasting_id` and `score`. Rows with no key are skipped — an unplaced wine
    belongs on no board. A null `tasting_id` counts as its own evening, since an
    ad-hoc bottle wasn't poured alongside the others.
    """
    scores: dict[str, list[int]] = {}
    bottles: dict[str, set[int]] = {}
    evenings: dict[str, set[object]] = {}
    for row in rows:
        key = row["key"]
        if not key:
            continue
        scores.setdefault(key, []).append(row["score"])
        bottles.setdefault(key, set()).add(row["wine_id"])
        night = row["tasting_id"]
        evenings.setdefault(key, set()).add(
            night if night is not None else ("loose", row["wine_id"])
        )

    tallies = [
        Tally(key, len(bottles[key]), len(vals), len(evenings[key]), sum(vals) / len(vals))
        for key, vals in scores.items()
        if len(bottles[key]) >= minimum
    ]
    # Bottle count breaks ties, so the better-established of two equal averages
    # sits higher.
    return sorted(tallies, key=lambda t: (-t.average, -t.wines, t.name))


class Coverage(NamedTuple):
    field: str
    known: int
    total: int

    @property
    def share(self) -> float:
        return self.known / self.total if self.total else 0.0


def coverage(rows: Sequence[Row], fields: Sequence[str]) -> list[Coverage]:
    """How much of the cellar has each field filled in. One row per wine."""
    return [
        Coverage(field, sum(1 for row in rows if row[field] is not None), len(rows))
        for field in fields
    ]


def spread(tallies: Sequence[Tally]) -> float | None:
    """Best average minus worst, across a board.

    Small means the club rates everything much the same and the board is noise;
    large means it is actually saying something.
    """
    if len(tallies) < 2:
        return None
    return tallies[0].average - tallies[-1].average
