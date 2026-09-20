#!/usr/bin/env python3
"""Load the club's per-evening survey exports into the wine database.

    python scripts/import_surveys.py --dry-run    # report, write nothing
    python scripts/import_surveys.py              # do it

One file is one evening. Each is a single sheet, `All Data`: ten columns of
survey metadata, then a column per question with the wine's name in the header
and everyone's score beneath it, then the respondent's name last.

Several evenings put jokes among the bottles — "Var Robert god i kveld?",
"Tror du Tottenham klarer seg?" — so the wines have to be told from the rest.
Across every file the club has, each non-wine question ends in a question mark
and no wine name does, which is the whole of the rule.

Safe to re-run: evenings are keyed the same way `import_vinotek.py` keys them, a
wine already against an evening is updated rather than added twice, and a score
replaces the one it supersedes.

The exports live in data/, which is gitignored — they hold nine people's scores
and must not end up in the repo.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, vinotek, wine_origin  # noqa: E402
from bot.db import Database, utcnow_iso  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = REPO / "data" / "2025-2026"
DEFAULT_WORKBOOK = REPO / "data" / "FC Vino Vinotek.xlsx"

# Rows written by an import carry user id 0, which no Discord account can have,
# so it stays obvious where they came from. Same sentinel import_vinotek uses.
IMPORT_USER_ID = 0

# The survey's own columns, before the questions start.
METADATA_COLUMNS = 10
DATE_COLUMN = 4

# Two scores in United Grapes of America were typed with a stuck key, in an
# evening where every other score is 85–93. Recorded as the 88 they plainly
# meant rather than dropped, on the club's say-so — and reported when it fires,
# so the repair is never silent.
SCORE_REPAIRS = {88787: 88, 8888: 88}

# An evening whose survey recorded who brought each bottle instead of what it
# was.
BRINGER_LABELLED = "min hvite favoritt"

# "7. Vin 7" is an ordering prefix and nothing else, so it cleans away to an
# empty string — the survey never said what was in the glass. Such a column is
# not a bottle, and an evening made entirely of them waits for a names file
# rather than putting nameless wine in a cellar of 1156.

# Dates the club wrote into a filename, in the forms they used.
FILENAME_DATE = re.compile(
    r"""(\s+\d{1,2}[._]\d{1,2}[-_]\d{2,4}$)          # 7_9-26
      | (\s+\d{1,2}\.\s*\w+\s+\d{4}$)                # 8. juni 2026
      | (^\d{1,2}\s+\d{4}\s+)                        # 03 2025 …
      | (^\w+\s+\d{4}\s+)                            # januar 2025 …
    """,
    re.VERBOSE,
)


def fold(text: str) -> str:
    """Compare names without caring about case or accents."""
    stripped = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in stripped if not unicodedata.combining(c)).strip()


class Evening:
    """One tasting, read out of one file, before anything is written."""

    def __init__(self, theme: str, when: datetime.date, source: str) -> None:
        self.theme = theme
        self.when = when
        self.source = source
        self.wines: list[dict] = []      # name, vintage, brought_by, scores{member: n}

    @property
    def key(self) -> str:
        period = vinotek.Period(self.when.year, self.when.month)
        return vinotek.tasting_key(period, self.theme)

    @property
    def raters(self) -> set[str]:
        return {m for w in self.wines for m in w["scores"]}


def theme_from_filename(path: Path) -> str:
    """The evening's name, as the club wrote it on the file."""
    stem = path.stem
    stem = re.sub(r"^Export\s+FC\s+Vino\s+", "", stem, flags=re.I)
    stem = FILENAME_DATE.sub("", stem).strip(" -_")
    return stem


def is_wine_column(header: str, name_column: str) -> bool:
    """A bottle, rather than a joke or the box you put your own name in."""
    header = (header or "").strip()
    if not header or header == name_column:
        return False
    return not header.endswith("?") and not re.search(r"\bnavn\b", header, re.I)


def read_names(path: Path) -> dict[int, dict]:
    """`<export>.names.json`: what the survey's columns actually were.

    Some evenings are written up elsewhere and the survey only numbers them. The
    file maps a column to a bottle and who brought it; a column it does not
    mention is not imported, which is how an evening's unexplained columns are
    left out without a second mechanism for leaving things out.
    """
    sidecar = path.with_suffix(path.suffix + ".names.json")
    if not sidecar.exists():
        return {}
    listed = json.loads(sidecar.read_text(encoding="utf-8"))
    return {
        int(column): entry
        for column, entry in listed.items()
        if column.isdigit() and isinstance(entry, dict)
    }


def read_export(path: Path, notes: list[str]) -> Evening | None:
    import openpyxl

    book = openpyxl.load_workbook(path, data_only=True)
    rows = list(book[book.sheetnames[0]].iter_rows(values_only=True))
    if len(rows) < 2:
        notes.append(f"{path.name}: no responses")
        return None

    header = [("" if c is None else str(c)).strip() for c in rows[0]]
    name_column = next(
        (h for h in reversed(header) if re.search(r"\bnavn\b", h, re.I)), ""
    )
    name_index = header.index(name_column) if name_column else len(header) - 1

    # Oldest first, so a card submitted twice leaves the later one standing —
    # the same reading `/wine rate` gives a second score.
    body = sorted(
        (r for r in rows[1:] if any(c is not None for c in r)),
        key=lambda r: str(r[DATE_COLUMN] or ""),
    )
    stamps = [str(r[DATE_COLUMN])[:10] for r in body if r[DATE_COLUMN]]
    if not stamps:
        notes.append(f"{path.name}: no submission dates, cannot place it in time")
        return None
    when = datetime.date.fromisoformat(min(stamps))

    theme = theme_from_filename(path)
    evening = Evening(theme, when, path.name)
    bringers = fold(theme) == fold(BRINGER_LABELLED)
    known = {fold(m): m for m in vinotek.MEMBERS}
    named = read_names(path)

    position, bare = 0, 0
    for index in range(METADATA_COLUMNS, len(header)):
        if index == name_index or not is_wine_column(header[index], name_column):
            continue
        position += 1
        label = wine_origin.clean_name(header[index])
        brought_by = None

        if named:
            # A names file is the whole truth about this evening: a column it
            # does not mention was not a wine anybody wrote down.
            entry = named.get(position)
            if entry is None:
                continue
            label = entry["name"]
            brought_by = entry.get("brought_by")
        elif not label:
            bare += 1
            continue
        elif bringers and fold(label) in known:
            # "Vin 3 HÅVARD" is who brought it, not what it was.
            brought_by = known[fold(label)]
            label = f"{brought_by}'s white"
        elif bringers:
            label = label.title()

        scores: dict[str, int] = {}
        for row in body:
            who = str(row[name_index] or "").strip()
            member = known.get(fold(who))
            if member is None:
                if who:
                    notes.append(f"{path.name}: no member called {who!r}")
                continue
            raw = row[index]
            if raw in SCORE_REPAIRS:
                notes.append(
                    f"{path.name}: read {raw} as {SCORE_REPAIRS[raw]} "
                    f"for {member} on {label!r}"
                )
                raw = SCORE_REPAIRS[raw]
            score = vinotek.normalise_score(raw, scale=1)
            if score is not None:
                scores[member] = score        # later card wins, rows are in order

        evening.wines.append({
            "name": label,
            "vintage": wine_origin.parse_vintage(label),
            "brought_by": brought_by,
            "scores": scores,
        })

    if bare and evening.wines:
        notes.append(f"{path.name}: {bare} column(s) named no wine and were left out")
    if bare and not evening.wines:
        notes.append(
            f"{path.name}: skipped — its {bare} columns name no wine, only "
            "'Vin 1'…. Write a <export>.names.json beside it and it will be read."
        )
        return None
    return evening


def read_folder(folder: Path, notes: list[str]) -> list[Evening]:
    evenings = []
    for path in sorted(folder.glob("*.xlsx")):
        # `~$…` is Excel's lock file for a workbook somebody has open, and
        # `._…` is a macOS sidecar. Neither is a spreadsheet.
        if path.name.startswith(("~$", "._")):
            continue
        evening = read_export(path, notes)
        if evening is not None:
            evenings.append(evening)
    return evenings


def read_workbook_2025(path: Path, known_keys: set[str], notes: list[str]) -> list[Evening]:
    """The 2025 sheet of the old workbook, for evenings the folder hasn't got.

    No import has ever read it — `vinotek.SHEETS` stops at 2016 and Alltime — so
    August 2025 is in there and nowhere else. November and December are in the
    folder too, and must not arrive twice from two sources.
    """
    import openpyxl

    if not path.exists():
        notes.append(f"no workbook at {path}, so nothing from its 2025 sheet")
        return []
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if "2025" not in book.sheetnames:
        return []

    spec = vinotek.SheetSpec("2025", 1, None)
    rows = list(book["2025"].iter_rows(values_only=True))
    parsed = []
    for block in vinotek.locate_blocks(list(rows[0])):
        for row in rows[1:]:
            wine, _ = vinotek.parse_row(row, block, spec, month_only_year=None)
            if wine is not None and wine.period is not None:
                parsed.append(wine)

    by_evening: dict[str, Evening] = {}
    for wine in parsed:
        when = datetime.date(wine.period.year, wine.period.month or 1, 1)
        evening = by_evening.setdefault(
            vinotek.tasting_key(wine.period, wine.theme),
            Evening(wine.theme or "", when, f"{path.name} (2025 sheet)"),
        )
        evening.wines.append({
            "name": wine.name,
            "vintage": wine_origin.parse_vintage(wine.name),
            "brought_by": wine.brought_by,
            "scores": dict(wine.scores),
        })

    fresh = []
    for key, evening in by_evening.items():
        if key in known_keys:
            notes.append(f"workbook 2025: {evening.theme!r} already read from the folder")
            continue
        fresh.append(evening)
    return fresh


def write(db: Database, evenings: list[Evening], *, dry_run: bool) -> dict:
    counts: collections.Counter = collections.Counter()
    members: dict[str, int] = {}
    for name in vinotek.MEMBERS:
        row = db.query_one("SELECT id FROM wine_members WHERE name=?", (name,))
        if row is not None:
            members[name] = row["id"]

    for evening in evenings:
        counts["tastings"] += 1
        if dry_run:
            counts["wines"] += len(evening.wines)
            counts["ratings"] += sum(len(w["scores"]) for w in evening.wines)
            continue

        db.execute(
            """INSERT INTO tastings (key, year, month, theme, added_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET theme=excluded.theme""",
            (evening.key, evening.when.year, evening.when.month,
             evening.theme or None, utcnow_iso()),
        )
        tasting_id = db.query_one(
            "SELECT id FROM tastings WHERE key=?", (evening.key,)
        )["id"]

        for wine in evening.wines:
            # Who brought it is part of which bottle this is. Two people can
            # turn up with the same wine — and at a blind tasting it gets poured
            # and scored twice — so matching on the name alone would fold the
            # second onto the first and overwrite its scores.
            existing = db.query_one(
                """SELECT id FROM wines
                   WHERE tasting_id=? AND LOWER(name)=LOWER(?)
                     AND IFNULL(brought_by,'')=IFNULL(?,'')""",
                (tasting_id, wine["name"], wine["brought_by"]),
            )
            if existing:
                wine_id = existing["id"]
                db.execute(
                    """UPDATE wines SET vintage=COALESCE(?, vintage),
                                        brought_by=COALESCE(?, brought_by) WHERE id=?""",
                    (wine["vintage"], wine["brought_by"], wine_id),
                )
            else:
                wine_id = db.execute(
                    """INSERT INTO wines (name, vintage, tasting_id, brought_by,
                                          added_by, added_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (wine["name"], wine["vintage"], tasting_id, wine["brought_by"],
                     IMPORT_USER_ID, utcnow_iso()),
                )
                counts["wines"] += 1

            for member, score in wine["scores"].items():
                if member not in members:
                    continue
                db.execute(
                    """INSERT INTO wine_ratings (wine_id, member_id, score, rated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(wine_id, member_id) DO UPDATE SET
                           score=excluded.score, rated_at=excluded.rated_at""",
                    (wine_id, members[member], score, utcnow_iso()),
                )
                counts["ratings"] += 1
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--no-workbook", action="store_true",
                        help="the folder only, skipping the old workbook's 2025 sheet")
    args = parser.parse_args(argv)

    if not args.folder.exists():
        print(f"No folder at {args.folder}")
        return 2

    notes: list[str] = []
    evenings = read_folder(args.folder, notes)
    if not args.no_workbook:
        evenings += read_workbook_2025(
            args.workbook, {e.key for e in evenings}, notes
        )
    evenings.sort(key=lambda e: e.when)

    print(f"{len(evenings)} evening(s) read\n")
    for evening in evenings:
        scored = sum(len(w["scores"]) for w in evening.wines)
        print(f"  {evening.when}  {evening.theme[:38]:<38} "
              f"{len(evening.wines):>2} wines  {len(evening.raters)} raters  {scored:>3} scores")

    if notes:
        print("\nnotes:")
        for note in notes:
            print(f"  - {note}")

    cfg = config.load(require_discord=False)
    db = Database(cfg.db_path)
    db.connect()
    before = db.query_one(
        """SELECT (SELECT COUNT(*) FROM tastings) AS tastings,
                  (SELECT COUNT(*) FROM wines) AS wines,
                  (SELECT COUNT(*) FROM wine_ratings) AS ratings"""
    )
    counts = write(db, evenings, dry_run=args.dry_run)
    after = db.query_one(
        """SELECT (SELECT COUNT(*) FROM tastings) AS tastings,
                  (SELECT COUNT(*) FROM wines) AS wines,
                  (SELECT COUNT(*) FROM wine_ratings) AS ratings"""
    )
    db.close()

    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb}: {counts.get('tastings', 0)} tastings, "
          f"{counts.get('wines', 0)} wines, {counts.get('ratings', 0)} ratings")
    if not args.dry_run:
        print(f"database: {before['tastings']} → {after['tastings']} tastings, "
              f"{before['wines']} → {after['wines']} wines, "
              f"{before['ratings']} → {after['ratings']} ratings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
