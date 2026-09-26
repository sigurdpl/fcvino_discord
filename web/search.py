"""Searching the wine archive.

Pure: no database, no FastAPI, no Jinja. `web/queries.py` reads the rows and
`web/routes/wine.py` renders them; everything about *what matches what* lives
here so it can be tested against real names from the archive.

**Why not FTS5.** SQLite here has it, but the archive is 1016 wines: the whole
index rebuilds in a few milliseconds, so a virtual table would buy nothing a
reader could feel while adding a synchronisation surface — and a schema change
to the database the bot owns. If the cellar ever reaches a size where a linear
scan shows, this module is the only thing that has to change.

**Why fold everything.** The spreadsheet spells things every way a Norwegian
keyboard allows: `Côte-Rôtie` and `Cote Rotie`, `Spätburgunder` and
`Spatburgunder`, `Sør Afrika` where NFKD alone leaves the ø intact. Searching
has exactly the problem `wine_origin.fold` was written for, so it is reused
rather than reinvented — a query folds the same way the index did.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

from bot.wine_origin import fold

# Ranking bands. A wine *named* Barolo beats one merely poured at a Barolo
# evening, which is the difference between a useful search and a list.
NAME_PREFIX = 0
NAME_WORD = 1
NAME_ANYWHERE = 2
ORIGIN = 3
ELSEWHERE = 4

DEFAULT_LIMIT = 50

# A wine nobody has scored. Shared and unwritable, so the default cannot be
# filled in by accident on one row and turn up on every other.
NO_SCORES: Mapping[str, int] = MappingProxyType({})


class Row(NamedTuple):
    """One wine, flattened for searching. Built by `web/queries.py`."""

    id: int
    name: str
    country: str | None
    region: str | None
    grape: str | None
    vintage: int | None
    theme: str | None
    year: int | None
    brought_by: str | None
    average: float | None
    ratings: int
    # Who gave it what. The average is nine opinions flattened into one number;
    # this is the nine, which is what "does Tore actually like this" needs.
    scores: Mapping[str, int] = NO_SCORES


class Hit(NamedTuple):
    row: Row
    band: int

    @property
    def id(self) -> int:
        return self.row.id


@dataclass(frozen=True)
class Filters:
    """The dropdowns beside the search box. Every field is optional."""

    country: str | None = None
    region: str | None = None
    grape: str | None = None
    # Who carried the bottle in, and — quite separately — whose opinion of it
    # to look at. Both used to be spelled "member", which is a trap.
    brought_by: str | None = None
    rated_by: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    vintage_from: int | None = None
    vintage_to: int | None = None
    min_score: float | None = None

    @property
    def empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in self.__dataclass_fields__.values())

    def matches(self, row: Row) -> bool:
        if self.country and row.country != self.country:
            return False
        if self.region and row.region != self.region:
            return False
        if self.grape and row.grape != self.grape:
            return False
        if self.brought_by and row.brought_by != self.brought_by:
            return False
        if self.rated_by and self.rated_by not in row.scores:
            return False
        if self.year_from is not None and (row.year is None or row.year < self.year_from):
            return False
        if self.year_to is not None and (row.year is None or row.year > self.year_to):
            return False
        if self.vintage_from is not None and (
            row.vintage is None or row.vintage < self.vintage_from
        ):
            return False
        if self.vintage_to is not None and (row.vintage is None or row.vintage > self.vintage_to):
            return False
        if self.min_score is not None and self.yardstick(row) is None:
            return False
        return True

    def yardstick(self, row: Row) -> float | None:
        """The score `min_score` is measured against, when it is high enough.

        With a member picked it is *their* score — "Tore, at least 90" asks for
        the wines Tore gave 90, not the ones the room did. With nobody picked
        it is the average, exactly as it always was.
        """
        mark = row.scores.get(self.rated_by) if self.rated_by else row.average
        if mark is None or (self.min_score is not None and mark < self.min_score):
            return None
        return mark


QUOTED = re.compile(r'"([^"]*)"')


def tokenize(query: str) -> list[str]:
    """Split a query into folded tokens, keeping "quoted phrases" whole.

    A phrase in quotes is one token, so `"vino nobile"` will not also match a
    wine that says `nobile` somewhere and `vino` somewhere else.
    """
    phrases = [fold(m).strip() for m in QUOTED.findall(query)]
    rest = QUOTED.sub(" ", query)
    words = [w for w in fold(rest).split() if w]
    return [t for t in (*phrases, *words) if t]


class Index:
    """Folded text for every wine, built once and asked many times."""

    def __init__(self, rows: Iterable[Row]) -> None:
        self.rows: list[Row] = list(rows)
        self._name: list[str] = []
        self._origin: list[str] = []
        self._all: list[str] = []
        for row in self.rows:
            name = fold(row.name)
            origin = fold(
                " ".join(
                    str(part)
                    for part in (row.country, row.region, row.grape, row.vintage)
                    if part is not None
                )
            )
            # The evening is searchable too: "hvite perler" or "landskamp" is
            # how someone actually remembers a bottle.
            everything = fold(
                " ".join(
                    str(part)
                    for part in (
                        row.name, row.country, row.region, row.grape,
                        row.vintage, row.theme, row.year, row.brought_by,
                    )
                    if part is not None
                )
            )
            self._name.append(name)
            self._origin.append(origin)
            self._all.append(everything)

    def __len__(self) -> int:
        return len(self.rows)

    def _band(self, i: int, tokens: Sequence[str]) -> int | None:
        """How well this wine matches, or None if it doesn't.

        Every token must appear somewhere; the band is decided by where the
        *worst-placed* token landed, so a wine only counts as a name match when
        the whole query is in its name.
        """
        name, origin, everything = self._name[i], self._origin[i], self._all[i]
        worst = NAME_PREFIX
        for token in tokens:
            if token not in everything:
                return None
            if name.startswith(f" {token}"):
                band = NAME_PREFIX
            elif f" {token} " in name:
                band = NAME_WORD
            elif token in name:
                band = NAME_ANYWHERE
            elif token in origin:
                band = ORIGIN
            else:
                band = ELSEWHERE
            worst = max(worst, band)
        return worst

    def search(
        self,
        query: str = "",
        filters: Filters | None = None,
        *,
        limit: int | None = DEFAULT_LIMIT,
    ) -> list[Hit]:
        """Wines matching every token of `query` and all of `filters`.

        An empty query is not an error: with filters it means "everything in
        this country", and with neither it means "the archive", best first.
        """
        filters = filters or Filters()
        tokens = tokenize(query)

        hits: list[Hit] = []
        for i, row in enumerate(self.rows):
            if not filters.matches(row):
                continue
            band = ELSEWHERE if not tokens else self._band(i, tokens)
            if band is None:
                continue
            hits.append(Hit(row, band))

        # Band first, then the wines the club actually has an opinion about:
        # more ratings, then a higher average. Name last so it is deterministic.
        # With a member picked, their own score decides the order within the
        # band — the whole question being "what does this person like".
        if filters.rated_by:
            hits.sort(key=lambda h: (h.band, -h.row.scores.get(filters.rated_by, 0),
                                     -(h.row.average or 0), h.row.name))
        else:
            hits.sort(key=lambda h: (h.band, -h.row.ratings, -(h.row.average or 0), h.row.name))
        return hits[:limit] if limit is not None else hits

    def raters(self) -> list[tuple[str, int]]:
        """Everyone who has scored something, and how many, busiest first.

        The same job `facets` does for the other dropdowns, but a wine holds
        several scores rather than one value, so it cannot go through that.

        Guests are here too. Marius has 22 ratings and is deliberately not
        counted as a member anywhere else, but this dropdown asks whose opinion
        to look at rather than who is in the club, and hiding 22 real scores
        would be the stranger answer.
        """
        counts: dict[str, int] = {}
        for row in self.rows:
            for who in row.scores:
                counts[who] = counts.get(who, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def facets(self, field: str) -> list[tuple[str, int]]:
        """Distinct values of `field` with counts, commonest first.

        Feeds the filter dropdowns, so they only ever offer a choice that
        actually returns something.
        """
        counts: dict[str, int] = {}
        for row in self.rows:
            value = getattr(row, field)
            if value is not None:
                counts[str(value)] = counts.get(str(value), 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
