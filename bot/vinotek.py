"""Reading the club's tasting spreadsheet.

Pure functions over cell values — no openpyxl, no database — so every rule here
is unit-testable. `scripts/import_vinotek.py` does the file reading and the
writing; this module decides what the cells mean.

The workbook grew by hand over thirteen years, so the rules encode real mess:
eight different ways of writing a month, a year typed as 2323, two scoring
scales, and a header cell overwritten with a theme name. Each of those is
measured behaviour, not defensive guesswork.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from .wine_origin import clean_name

# The members, in the order their columns appear. Marius only has columns in the
# 2013 and 2014 sheets, and only a handful of scores — but including him is what
# makes the scores read here add up to those sheets' own Sum column exactly, so
# they are unquestionably his.
MEMBERS = (
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
    "Marius",
)

# Norwegian month names as actually typed in the sheet, abbreviations, full
# names and the odd misspelling ("Setember").
MONTHS: dict[str, int] = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mar": 3, "mars": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "setember": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "des": 12, "desember": 12,
}

# 72 rows carry this. It is a typo for 2023, and correcting it merges a tasting
# that is otherwise split across "Møte 03 2323" and "Møte 03 2023".
YEAR_TYPOS = {2323: 2023}

# The sheet's structural column headers, i.e. everything that isn't a member.
STRUCTURAL = ("Tid", "Ansvarlig", "Tema", "Sted", "Vin", "Pris", "Poeng")

# A row whose wine name is this is a per-member average, not a wine.
SUMMARY_MARKERS = {"snitt", "gjennomsnitt", "sum", "total"}


class Period(NamedTuple):
    """When a tasting happened, to the precision the sheet actually recorded."""

    year: int
    month: int | None

    def key(self) -> str:
        return f"{self.year}-{self.month:02d}" if self.month else f"{self.year}-00"


class Columns(NamedTuple):
    """Where each field lives in a given sheet. Positions differ per sheet."""

    tid: int | None
    responsible: int | None
    theme: int | None
    location: int | None
    wine: int
    price: int | None
    stated_average: int | None
    members: dict[str, int]
    # The sheet's own Sum of the member scores. A far better check than Poeng,
    # which is a plain mean in Alltime but a trimmed one (drop the highest and
    # lowest) in the early sheets, and something else again in 2016.
    total: int | None


class SheetSpec(NamedTuple):
    sheet: str
    scale: int  # 10 for the out-of-ten era, 1 for the out-of-a-hundred one
    default_year: int | None  # the 2013 sheet has no date column at all


# Score scales: 2013–2016 marked out of ten, 2017 onward out of a hundred.
# Multiplying the early era by ten puts both on one scale, which is what makes
# an all-time ranking mean anything.
SHEETS = (
    SheetSpec("2013", 10, 2013),
    SheetSpec("2014", 10, None),
    SheetSpec("2015", 10, None),
    SheetSpec("2016", 10, None),
    SheetSpec("Alltime", 1, None),
)


def locate_blocks(header: list[Any]) -> list[Columns]:
    """Split a header row into the tables it contains, left to right.

    The 2016 sheet holds **two** tables side by side — columns 0–19 and 22–41,
    each with its own Tid, Tema, Vin, member columns and Sum, covering different
    tastings. Reading only one of them silently loses half that year.

    A new table starts wherever a `Tid` header appears again (or `Ansvarlig`, for
    the 2013 sheet which has no date column at all).
    """
    names = [("" if cell is None else str(cell)).strip() for cell in header]
    starts = [i for i, name in enumerate(names) if name == "Tid"]
    if not starts:
        starts = [i for i, name in enumerate(names) if name == "Ansvarlig"]
    if not starts:
        starts = [0]

    blocks: list[Columns] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(names)
        window = list(header[start:end])
        if not any(("" if c is None else str(c)).strip() == "Vin" for c in window):
            continue
        block = locate_columns(window)
        blocks.append(_shift(block, start))
    return blocks


def _shift(columns: Columns, offset: int) -> Columns:
    """Move a block's column positions into whole-row coordinates."""
    move = lambda position: None if position is None else position + offset  # noqa: E731
    return Columns(
        tid=move(columns.tid),
        responsible=move(columns.responsible),
        theme=move(columns.theme),
        location=move(columns.location),
        wine=columns.wine + offset,
        price=move(columns.price),
        stated_average=move(columns.stated_average),
        members={name: position + offset for name, position in columns.members.items()},
        total=move(columns.total),
    )


def locate_columns(header: list[Any]) -> Columns:
    """Find each field by header name, falling back to position where needed.

    Names rather than fixed positions because the sheets disagree: 2013 has no
    `Tid` at all and 2015 inserts a `Pris` column that shifts everything right.

    One header is simply wrong — the 2016 sheet's theme column is titled
    "Top of the pops", the name of a tasting. When `Tema` is missing, the theme
    is taken as the column after `Ansvarlig`, which is where every sheet puts it.
    """
    names = [("" if cell is None else str(cell)).strip() for cell in header]
    index = {name: position for position, name in enumerate(names) if name}

    if "Vin" not in index:
        raise ValueError(f"no 'Vin' column in header: {names[:8]}")

    theme = index.get("Tema")
    if theme is None and "Ansvarlig" in index:
        theme = index["Ansvarlig"] + 1

    return Columns(
        tid=index.get("Tid"),
        responsible=index.get("Ansvarlig"),
        theme=theme,
        location=index.get("Sted"),
        wine=index["Vin"],
        price=index.get("Pris"),
        stated_average=index.get("Poeng"),
        members={name: index[name] for name in MEMBERS if name in index},
        total=index.get("Sum"),
    )


def parse_period(value: Any, *, month_only_year: int | None = None) -> Period | None:
    """Read a `Tid` cell. Returns None when it says nothing usable.

    Handles every form the workbook contains:

    - a real date cell — the day is discarded, because every date in the
      workbook is the 1st and so no day was ever really recorded
    - `okt18`, `Sept19`, `April22`, `juli24` — month name plus 2-digit year
    - `August 2021`, `Setember 2021` — month name plus 4-digit year
    - `0524` … `1224` — MMYY
    - `Møte 03 2023` — meeting number and year, including the 2323 typo
    - `Oktober` — a month with no year at all, dated by `month_only_year`
    """
    if isinstance(value, datetime.datetime):
        return Period(value.year, value.month)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    meeting = re.fullmatch(r"Møte\s+(\d{1,2})\s+(\d{4})", text, re.IGNORECASE)
    if meeting:
        year = int(meeting.group(2))
        return Period(YEAR_TYPOS.get(year, year), int(meeting.group(1)))

    if re.fullmatch(r"\d{4}", text):  # MMYY
        month, year = int(text[:2]), 2000 + int(text[2:])
        if 1 <= month <= 12:
            return Period(year, month)
        return None

    named = re.fullmatch(r"([A-Za-zÀ-ÿ]+)\s*(\d{2,4})", text)
    if named:
        month = MONTHS.get(named.group(1).lower())
        if month:
            year = int(named.group(2))
            year = year + 2000 if year < 100 else year
            return Period(YEAR_TYPOS.get(year, year), month)

    month = MONTHS.get(text.lower())
    if month and month_only_year:
        return Period(month_only_year, month)
    return None


def normalise_score(value: Any, *, scale: int) -> int | None:
    """A member's score on the 1–100 scale, or None if they weren't there.

    An empty cell means the member didn't attend. A zero is treated the same
    way: the constraint is 1–100, and the three zeros in the workbook are far
    likelier to be slips than a verdict of nought.
    """
    if value is None or isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    scaled = round(number * scale)
    return max(1, min(100, scaled))


def looks_like_summary(wine: Any) -> bool:
    """True for the row whose "scores" are per-member averages."""
    return isinstance(wine, str) and wine.strip().lower() in SUMMARY_MARKERS


def clean_text(value: Any) -> str | None:
    """Trim a text cell, treating whitespace-only as absent.

    Non-breaking spaces appear in wine names pasted from websites, so they are
    folded to ordinary spaces rather than left to break comparisons later.
    """
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").replace("​", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def clean_wine_name(value: Any) -> str | None:
    """A wine's name with the pouring-order label removed.

    Done here, where the cell is first read, rather than in a later pass: the
    name is the identity `scripts/import_vinotek.py` matches a wine on, so
    rewriting it afterwards would make the next import insert duplicates instead
    of updating.
    """
    text = clean_text(value)
    return clean_name(text) or None if text else None


def tasting_key(period: Period | None, theme: str | None) -> str:
    """A stable identity for a tasting: when it happened and what it was about.

    Deliberately *not* including the location. On a bring-your-own night the
    `Sted` column records whose bottle each row is rather than where everyone
    was sitting, so keying on it splits one December evening into ten tastings.
    Theme plus month is what identifies an evening.
    """
    when = period.key() if period else "unknown"
    return f"{when}|{(theme or '').lower()}"


class ParsedWine(NamedTuple):
    period: Period | None
    theme: str | None
    location: str | None
    brought_by: str | None
    name: str
    price_nok: int | None
    scores: dict[str, int]
    stated_average: float | None
    stated_total: float | None

    @property
    def signature(self) -> tuple:
        """Identity for cross-sheet duplicate detection.

        The January 2014 "Top of the pops" tasting is recorded in both the 2013
        and 2014 sheets — ten rows with the same wine and, once the scales are
        normalised, the same scores. Comparing the name and every score is what
        catches that without touching genuine re-tastings of the same bottle,
        which score differently.
        """
        return (self.name.lower(), tuple(sorted(self.scores.items())))


def parse_row(
    row: tuple, columns: Columns, spec: SheetSpec, *, month_only_year: int | None = None
) -> tuple[ParsedWine | None, str | None]:
    """Turn one spreadsheet row into a wine, or explain why it isn't one.

    Returns (wine, None) or (None, reason). Reasons are reported by the importer
    rather than swallowed, so a sheet growing a new kind of junk row shows up.
    """

    def cell(position: int | None) -> Any:
        if position is None or position >= len(row):
            return None
        return row[position]

    name = clean_wine_name(cell(columns.wine))
    if not name:
        return None, "no wine name"
    if looks_like_summary(cell(columns.wine)):
        return None, "summary row"

    scores = {
        member: score
        for member, position in columns.members.items()
        if (score := normalise_score(cell(position), scale=spec.scale)) is not None
    }
    if not scores:
        return None, "no scores"

    period = parse_period(cell(columns.tid), month_only_year=month_only_year)
    if period is None and spec.default_year:
        period = Period(spec.default_year, None)

    price = None
    raw_price = cell(columns.price)
    if isinstance(raw_price, (int, float)) and raw_price > 0:
        price = round(raw_price)

    stated = cell(columns.stated_average)
    try:
        stated_average = float(stated) if stated is not None else None
    except (TypeError, ValueError):
        stated_average = None

    raw_total = cell(columns.total)
    stated_total = float(raw_total) if isinstance(raw_total, (int, float)) else None

    return (
        ParsedWine(
            period=period,
            theme=clean_text(cell(columns.theme)),
            location=clean_text(cell(columns.location)),
            brought_by=clean_text(cell(columns.responsible)),
            name=name,
            price_nok=price,
            scores=scores,
            stated_average=stated_average,
            stated_total=stated_total,
        ),
        None,
    )


# -- undoing Excel's fill handle --------------------------------------------

# Dragging a cell down in Excel does not copy it — it *extends the series*, so a
# theme of "Top of the pops 2021" becomes 2022, 2023, 2024 on the rows below, and
# a date of "juli24" becomes juli25, juli26. Three evenings in the workbook were
# filled in this way, and the sheet's own arithmetic can't catch it because every
# score is still correct; only the label of the evening is wrong.
#
# The signature is unmistakable and does not occur naturally: several rows that
# agree on everything else, whose one differing field steps by exactly one, and
# where each step carries exactly one wine. A real recurring evening — the club
# genuinely holds a "Top of the pops" every January — pours eight or ten bottles
# per year, never one.
FILL_RUN = 4

# A dragged block can have a row deleted from the middle afterwards, leaving a
# hole in the series: the 2018 Gevrey-Chambertin evening runs February to
# September with April missing, because that wine is in the per-year sheet but
# not in Alltime. One hole still reads as a drag; two and the run is not dense
# enough to be sure, so it is left alone.
FILL_GAP = 1

THEME_TRAILING_YEAR = re.compile(r"^(?P<stem>.*?)(?P<year>\d{4})\s*$")


def _is_fill_run(values: Sequence[int]) -> bool:
    """A dense ascending run: at least FILL_RUN values, at most FILL_GAP missing."""
    ordered = sorted(set(values))
    if len(ordered) < FILL_RUN:
        return False
    span = ordered[-1] - ordered[0] + 1
    return span - len(ordered) <= FILL_GAP


def _split_theme_year(theme: str | None) -> tuple[str, int] | None:
    if not theme:
        return None
    m = THEME_TRAILING_YEAR.match(theme)
    if not m:
        return None
    return m.group("stem"), int(m.group("year"))


def undo_fill_handle(wines: Sequence[ParsedWine]) -> tuple[list[ParsedWine], list[str]]:
    """Collapse a dragged series back onto the value it was dragged from.

    Returns the repaired rows and a note for each evening put back together.
    Only ever moves a row *earlier* in the series, because the fill handle
    extends forwards from the cell that was typed.
    """
    repaired = list(wines)
    notes: list[str] = []

    def collapse(indices: dict[int, list[int]], describe, apply) -> None:
        """`indices` maps a step value to the rows carrying it."""
        if not _is_fill_run(list(indices)) or any(len(rows) != 1 for rows in indices.values()):
            return
        first = min(indices)
        for step, rows in indices.items():
            if step == first:
                continue
            for i in rows:
                repaired[i] = apply(repaired[i], first)
        notes.append(describe(first, max(indices), len(indices)))

    # A dragged theme: same evening, same place, the year in the theme stepping.
    by_stem: dict[tuple, dict[int, list[int]]] = {}
    for i, wine in enumerate(repaired):
        split = _split_theme_year(wine.theme)
        if split is None:
            continue
        stem, year = split
        key = (wine.period, stem.strip().lower(), (wine.location or "").lower())
        by_stem.setdefault(key, {}).setdefault(year, []).append(i)

    for (_, stem, _), years in list(by_stem.items()):
        collapse(
            years,
            lambda lo, hi, n, stem=stem: (
                f"theme {stem.strip()!r} was dragged {lo}→{hi}; "
                f"{n} rows are one evening, filed under {lo}"
            ),
            lambda wine, year: wine._replace(
                theme=f"{_split_theme_year(wine.theme)[0]}{year}"
            ),
        )

    # A dragged date: same theme, same place, the year or the month stepping.
    by_theme: dict[tuple, list[int]] = {}
    for i, wine in enumerate(repaired):
        if wine.period is None or not wine.theme:
            continue
        by_theme.setdefault((wine.theme.lower(), (wine.location or "").lower()), []).append(i)

    for (theme, _), rows in by_theme.items():
        months = {repaired[i].period.month for i in rows}
        years = {repaired[i].period.year for i in rows}

        if len(months) == 1:
            steps: dict[int, list[int]] = {}
            for i in rows:
                steps.setdefault(repaired[i].period.year, []).append(i)
            collapse(
                steps,
                lambda lo, hi, n, t=theme: (
                    f"date on {t!r} was dragged {lo}→{hi}; "
                    f"{n} rows are one evening, filed under {lo}"
                ),
                lambda wine, year: wine._replace(period=wine.period._replace(year=year)),
            )
        elif len(years) == 1 and None not in months:
            steps = {}
            for i in rows:
                steps.setdefault(repaired[i].period.month, []).append(i)
            collapse(
                steps,
                lambda lo, hi, n, t=theme: (
                    f"month on {t!r} was dragged {lo}→{hi}; "
                    f"{n} rows are one evening, filed under month {lo}"
                ),
                lambda wine, month: wine._replace(period=wine.period._replace(month=month)),
            )

    return repaired, notes
