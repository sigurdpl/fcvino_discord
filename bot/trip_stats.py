"""Statistics over the away-trips archive.

Every function here is pure: it takes rows and returns plain values, with no
database, no Discord and no clock. That is what makes the interesting questions
("which club have we seen most", "longest run of years") cheap to test.

Rows are read with `row["key"]` only, never `.get()`, so both `sqlite3.Row` and
plain dicts work — the tests pass dicts. Match rows are expected in the shape
`Database.trip_match_rows()` returns: the `trip_matches` columns plus `year`,
`country` and `trip_city` joined on.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, NamedTuple

Row = Any  # sqlite3.Row or dict — anything supporting row["key"]


class Goal(NamedTuple):
    minute: int | None
    scorer: str
    side: str | None  # "H", "A", or None when not recorded


# -- filtering ---------------------------------------------------------------


def played(matches: Sequence[Row]) -> list[Row]:
    """Matches with a recorded score.

    A fixture entered before anyone remembered the result still counts as a
    match we attended, but it cannot contribute to goal or result statistics.
    """
    return [m for m in matches if m["home_goals"] is not None and m["away_goals"] is not None]


# -- countries, cities, grounds ---------------------------------------------


def country_counts(trips: Sequence[Row]) -> list[tuple[str, int]]:
    """Countries visited, most-visited first, then alphabetically."""
    counter = Counter(t["country"] for t in trips if t["country"])
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def years(trips: Sequence[Row]) -> list[int]:
    return sorted({t["year"] for t in trips if t["year"] is not None})


def distinct_stadiums(matches: Sequence[Row]) -> list[str]:
    return sorted({m["stadium"] for m in matches if m["stadium"]}, key=str.lower)


def total_attendance(matches: Sequence[Row]) -> int | None:
    """Combined attendance, or None if no match has it recorded."""
    values = [m["attendance"] for m in matches if m["attendance"]]
    return sum(values) if values else None


# -- goals and results ------------------------------------------------------


def total_goals(matches: Sequence[Row]) -> int:
    return sum(m["home_goals"] + m["away_goals"] for m in played(matches))


def goals_per_game(matches: Sequence[Row]) -> float | None:
    scored = played(matches)
    if not scored:
        return None
    return total_goals(scored) / len(scored)


def result_split(matches: Sequence[Row]) -> tuple[int, int, int]:
    """(home wins, draws, away wins) among matches with a score."""
    home = draw = away = 0
    for m in played(matches):
        if m["home_goals"] > m["away_goals"]:
            home += 1
        elif m["home_goals"] < m["away_goals"]:
            away += 1
        else:
            draw += 1
    return home, draw, away


def margin(match: Row) -> int:
    return abs(match["home_goals"] - match["away_goals"])


def biggest_win(matches: Sequence[Row]) -> Row | None:
    """The most lopsided match attended. Draws cannot win; ties break on goals."""
    decisive = [m for m in played(matches) if margin(m) > 0]
    if not decisive:
        return None
    return max(decisive, key=lambda m: (margin(m), m["home_goals"] + m["away_goals"]))


def highest_scoring(matches: Sequence[Row]) -> Row | None:
    scored = played(matches)
    if not scored:
        return None
    return max(scored, key=lambda m: (m["home_goals"] + m["away_goals"], margin(m)))


def goalless(matches: Sequence[Row]) -> list[Row]:
    """The 0-0s we sat through."""
    return [m for m in played(matches) if m["home_goals"] == 0 and m["away_goals"] == 0]


def goals_by_year(matches: Sequence[Row]) -> list[tuple[int, int]]:
    """(year, goals seen) for each year with a scored match, best year first."""
    counter: Counter[int] = Counter()
    for m in played(matches):
        counter[m["year"]] += m["home_goals"] + m["away_goals"]
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


# -- clubs and competitions -------------------------------------------------


def club_counts(matches: Sequence[Row]) -> list[tuple[str, int]]:
    """Every club seen and how often, counting home and away appearances."""
    counter: Counter[str] = Counter()
    for m in matches:
        for club in (m["home"], m["away"]):
            if club:
                counter[club] += 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def repeat_clubs(matches: Sequence[Row]) -> list[tuple[str, int]]:
    return [(club, n) for club, n in club_counts(matches) if n > 1]


def competition_counts(matches: Sequence[Row]) -> list[tuple[str, int]]:
    counter = Counter(m["competition"] for m in matches if m["competition"])
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def top_scorers(goal_rows: Sequence[Row], limit: int = 5) -> list[tuple[str, int]]:
    """Players we have watched score, most goals first."""
    counter = Counter(g["scorer"] for g in goal_rows if g["scorer"])
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:limit]


# -- streaks ----------------------------------------------------------------


def longest_year_streak(year_list: Sequence[int]) -> tuple[int, int] | None:
    """The longest run of consecutive years, as (first, last).

    Ties go to the earliest run. Returns None for no years at all; a single
    year is a streak of one, i.e. (y, y).
    """
    ordered = sorted(set(year_list))
    if not ordered:
        return None
    best = (ordered[0], ordered[0])
    start = ordered[0]
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current != previous + 1:
            start = current
            continue
        if current - start > best[1] - best[0]:
            best = (start, current)
    return best


def missing_detail(match: Row) -> list[str]:
    """Which enrichment fields this match still lacks, for /trips missing."""
    gaps = []
    if match["match_date"] is None:
        gaps.append("date")
    if not match["competition"]:
        gaps.append("competition")
    if match["home_goals"] is None or match["away_goals"] is None:
        gaps.append("score")
    if not match["stadium"]:
        gaps.append("stadium")
    if not match["attendance"]:
        gaps.append("attendance")
    return gaps


# -- parsing ----------------------------------------------------------------

# "23 Haaland H"  /  "45+2 Foden (H)"  /  "81' Kane A"  /  "12 Odegaard"
GOAL_PATTERN = re.compile(
    r"""^\s*
        (?P<minute>\d{1,3})(?:\s*\+\s*\d{1,2})?\s*'?      # minute, optional stoppage
        \s+
        (?P<scorer>.+?)                                    # scorer, non-greedy
        (?:\s+\(?(?P<side>[HAha])\)?)?                     # optional H/A marker
        \s*$""",
    re.VERBOSE,
)

GOALS_HELP = 'Format: `23 Haaland H, 67 Foden H, 81 Kane A` — minute, scorer, then H or A.'


def parse_goals(text: str) -> tuple[list[Goal], list[str]]:
    """Parse a match's scorers from one string into rows, plus any rejects.

    Entering goals one slash command at a time would mean fifty commands for a
    single trip, so the whole match arrives as one field. Bad segments are
    returned rather than raised, so the good ones still get saved and the user
    is told exactly which bit was wrong.
    """
    goals: list[Goal] = []
    errors: list[str] = []
    for raw in re.split(r"[,;\n]", text or ""):
        segment = raw.strip()
        if not segment:
            continue
        match = GOAL_PATTERN.match(segment)
        if match is None:
            errors.append(segment)
            continue
        side = match.group("side")
        goals.append(
            Goal(
                minute=int(match.group("minute")),
                scorer=match.group("scorer").strip(),
                side=side.upper() if side else None,
            )
        )
    return goals, errors


# Short all-caps names are acronyms, not shouting.
ACRONYM_LIMIT = 4
# Words that stay lowercase inside a country name, but not as the first word.
LOWERCASE_WORDS = frozenset({"and", "of", "the", "y", "da", "de"})


def normalise_country(value: str) -> str:
    """Collapse casing and spacing so 'england' and 'England ' are one country.

    Without this, `/trips countries` would report England twice and "how many
    countries have we seen" would be wrong. Acronyms up to four characters are
    left intact (USA, UAE) and connector words stay lowercase, so we get
    'Bosnia and Herzegovina' rather than 'Bosnia And Herzegovina'.
    """
    cleaned = " ".join((value or "").split())
    if not cleaned:
        return cleaned
    if cleaned.isupper() and len(cleaned) <= ACRONYM_LIMIT:
        return cleaned

    words = []
    for index, word in enumerate(cleaned.split(" ")):
        if word.isupper() and len(word) <= ACRONYM_LIMIT:
            words.append(word)
        elif index and word.lower() in LOWERCASE_WORDS:
            words.append(word.lower())
        else:
            words.append(word[0].upper() + word[1:].lower())
    return " ".join(words)
