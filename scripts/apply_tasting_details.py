#!/usr/bin/env python3
"""Fill in where an evening was held, and who was responsible for it.

The survey exports say what was poured and what everyone thought of it, and
nothing about the evening itself. Both of those the club has written down
elsewhere, a line per evening:

    2023 - 12: Sigurd
    2025 01: Tore
    2026 04 Morten

A year, a month and a name — however it was punctuated. The month picks the
evening out, because the club holds one a month.

    python scripts/apply_tasting_details.py <file> --dry-run   # show the changes
    python scripts/apply_tasting_details.py <file>             # where it was held
    python scripts/apply_tasting_details.py <file> --responsible
    python scripts/apply_tasting_details.py <file> --overwrite  # let the file win

Safe to re-run: by default only an empty column is filled, and a second pass
with nothing to do reports nothing. Where the file disagrees with what is
already recorded it says so and leaves the database alone, unless you ask.

The files live in data/, which is gitignored.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, vinotek  # noqa: E402
from bot.db import Database  # noqa: E402

# Where it was held reads "at Tore's" on the evening's page; who was responsible
# reads "chosen by Tore". Two columns, one shape of file.
WHERE = "location"
RESPONSIBLE = "host"

# `2023 - 12: Sigurd`, `2025 01: Tore`, `2026 04 Morten` — the club has written
# it all three ways in one file, so the separators are all optional.
LINE = re.compile(r"^\s*(\d{4})\s*[-–—]?\s*(\d{1,2})\s*[:.\-]?\s*(.+?)\s*$")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.changes = 0
        self.problems: list[str] = []

    def change(self, message: str) -> None:
        self.lines.append(f"  ~ {message}")
        self.changes += 1

    def note(self, message: str) -> None:
        self.lines.append(f"    {message}")

    def problem(self, message: str) -> None:
        self.problems.append(message)


def read_file(path: Path, report: Report) -> list[tuple[int, int, str]]:
    """The file as (year, month, name), in the order it was written."""
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = LINE.match(line)
        if found is None:
            report.problem(f"line {number}: {line!r} is not a year, a month and a name")
            continue
        year, month, name = int(found[1]), int(found[2]), found[3].strip()
        if not 1 <= month <= 12:
            report.problem(f"line {number}: there is no month {month}")
            continue
        entries.append((year, month, name))
    return entries


def apply_file(
    db: Database,
    entries: list[tuple[int, int, str]],
    column: str,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    report: Report | None = None,
) -> Report:
    report = report or Report()
    known = {m.casefold() for m in vinotek.MEMBERS}

    for year, month, name in entries:
        label = f"{year}-{month:02d}"
        rows = db.query(
            "SELECT id, theme, location, host FROM tastings WHERE year=? AND month=?",
            (year, month),
        )
        if not rows:
            report.problem(f"{label}: no evening that month, so {name!r} has nowhere to go")
            continue
        if len(rows) > 1:
            # One a month is the club's habit, not a rule the schema enforces.
            # Guessing between two would put the wrong name on one of them.
            themes = ", ".join(r["theme"] or "—" for r in rows)
            report.problem(f"{label}: {len(rows)} evenings ({themes}), so it is ambiguous")
            continue

        row = rows[0]
        where = f"{label} {row['theme'] or '—'}"
        # Not refused, only pointed out: "Alle" and "Thomas & Sigurd" are both
        # already in this database and both are real answers.
        if name.casefold() not in known:
            report.note(f"{where}: {name!r} is not one member's name")

        current = (row[column] or "").strip()
        if current and not overwrite:
            if current != name:
                report.note(f"{where}: keeping {column}={current!r} (file says {name!r})")
            continue
        if current == name:
            continue

        was = f" (was {current!r})" if current else ""
        report.change(f"{where}: {column}={name!r}{was}")
        if not dry_run:
            db.execute(f"UPDATE tastings SET {column}=? WHERE id=?", (name, row["id"]))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="a line per evening: year, month, name")
    parser.add_argument("--responsible", action="store_true",
                        help="who chose the evening, rather than where it was held")
    parser.add_argument("--overwrite", action="store_true", help="let the file win")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"No file at {args.file}")
        return 2

    column = RESPONSIBLE if args.responsible else WHERE
    report = Report()
    entries = read_file(args.file, report)
    print(f"{len(entries)} line(s) read, filling {column}\n")

    cfg = config.load(require_discord=False)
    db = Database(cfg.db_path)
    db.connect()
    apply_file(db, entries, column, overwrite=args.overwrite,
               dry_run=args.dry_run, report=report)
    filled = db.query_one(
        f"SELECT COUNT(*) n FROM tastings WHERE IFNULL({column},'') <> ''"
    )["n"]
    total = db.query_one("SELECT COUNT(*) n FROM tastings")["n"]
    db.close()

    for line in report.lines:
        print(line)
    if report.problems:
        print("\nproblems:")
        for problem in report.problems:
            print(f"  - {problem}")

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb}: {report.changes} evening(s)")
    print(f"{column} now set on {filled} of {total} evenings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
