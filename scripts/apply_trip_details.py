#!/usr/bin/env python3
"""Apply researched trip detail from seeds/trip_details.json to the database.

The archive is entered by hand in Discord, then enriched here: exact dates,
competitions, stadiums, crowds and scorers, researched once and committed so the
detail survives a lost database.

    python scripts/apply_trip_details.py --dry-run     # show what would change
    python scripts/apply_trip_details.py               # fill empty columns only
    python scripts/apply_trip_details.py --create      # also insert missing trips
    python scripts/apply_trip_details.py --overwrite   # let the file win

Safe to re-run: a second pass with no changes reports nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.trip_stats import normalise_country, parse_goals  # noqa: E402

DEFAULT_SEED = Path(__file__).resolve().parent.parent / "seeds" / "trip_details.json"

# Columns the seed file may fill on an existing trip_matches row.
MATCH_FIELDS = (
    "match_date",
    "competition",
    "home_goals",
    "away_goals",
    "city",
    "stadium",
    "attendance",
    "notes",
)
# The country is fillable too: the trip is keyed on year alone, so the research
# pass can correct where we went as readily as when.
TRIP_FIELDS = ("country", "city", "date_from", "date_to", "notes")

# Seeded rows carry user id 0, which no Discord account can have, so it is
# obvious in the database that a row came from the file rather than a person.
SEED_USER_ID = 0


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


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _find_trip(db: Database, year: int):
    """Trips are keyed on year — one place a year."""
    return db.trip_by_year(year)


def _find_match(db: Database, trip_id: int, spec: dict[str, Any], claimed: set[int]):
    """Locate the stored match this spec refers to: teams first, then date.

    `claimed` holds the ids already bound to an earlier spec in this run. Without
    it, two matches on one trip both bind to whichever row exists first: the
    second spec finds no name or date match, falls back to "this trip has exactly
    one match", and overwrites the first instead of being created alongside it.
    """
    candidates = [
        row
        for row in db.query("SELECT * FROM trip_matches WHERE trip_id=?", (trip_id,))
        if row["id"] not in claimed
    ]
    home, away = (spec.get("home") or "").strip(), (spec.get("away") or "").strip()
    if home and away:
        named = [
            row
            for row in candidates
            if row["home"].lower() == home.lower() and row["away"].lower() == away.lower()
        ]
        if named:
            return named[0]
    if spec.get("match_date"):
        dated = [row for row in candidates if row["match_date"] == spec["match_date"]]
        if dated:
            return dated[0]
        # Fall through rather than giving up: the stored row may predate the
        # researched date, which is precisely what this pass is here to fix.
    # One unclaimed match left on the trip is unambiguous however it was spelled,
    # which is what lets "BVB vs FCB" be matched by its full names.
    return candidates[0] if len(candidates) == 1 else None


def _apply_columns(
    db: Database,
    table: str,
    row,
    spec: dict[str, Any],
    fields: tuple[str, ...],
    *,
    overwrite: bool,
    dry_run: bool,
    report: Report,
    label: str,
) -> None:
    updates: dict[str, Any] = {}
    for field in fields:
        if field not in spec:
            continue
        new = spec[field]
        if new is None:
            continue
        if not overwrite and not _is_empty(row[field]):
            if row[field] != new:
                report.note(f"{label}: keeping {field}={row[field]!r} (file says {new!r})")
            continue
        if row[field] != new:
            updates[field] = new
    if not updates:
        return
    report.change(f"{label}: " + ", ".join(f"{k}={v!r}" for k, v in updates.items()))
    if dry_run:
        return
    assignments = ", ".join(f"{k}=?" for k in updates)
    db.execute(
        f"UPDATE {table} SET {assignments} WHERE id=?", (*updates.values(), row["id"])
    )


def _apply_goals(
    db: Database,
    match_id: int,
    entries: list[str],
    *,
    overwrite: bool,
    dry_run: bool,
    report: Report,
    label: str,
) -> None:
    existing = db.query_one(
        "SELECT COUNT(*) AS n FROM trip_goals WHERE trip_match_id=?", (match_id,)
    )
    if existing and existing["n"] and not overwrite:
        report.note(f"{label}: {existing['n']} scorer(s) already recorded, left alone")
        return

    goals, rejects = parse_goals(", ".join(entries))
    for bad in rejects:
        report.problem(f"{label}: could not parse goal {bad!r}")
    if not goals:
        return
    report.change(f"{label}: {len(goals)} scorer(s)")
    if dry_run:
        return
    db.execute("DELETE FROM trip_goals WHERE trip_match_id=?", (match_id,))
    db.executemany(
        "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) VALUES (?, ?, ?, ?)",
        [(match_id, g.minute, g.scorer, g.side) for g in goals],
    )


def apply_seed(
    db: Database,
    payload: dict[str, Any],
    *,
    create: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> Report:
    report = Report()
    for spec in payload.get("trips", []):
        year, country = spec.get("year"), normalise_country(spec.get("country") or "")
        if not year or not country:
            report.problem(f"entry with no year/country: {spec!r}")
            continue
        label = f"{year} {country}"

        trip = _find_trip(db, year)
        if trip is None:
            if not create:
                report.problem(f"{label}: no {year} trip in the database (use --create)")
                continue
            report.change(f"{label}: creating trip")
            if not dry_run:
                db.upsert_trip(
                    year=year,
                    country=country,
                    city=spec.get("city"),
                    date_from=spec.get("date_from"),
                    date_to=spec.get("date_to"),
                    notes=spec.get("notes"),
                    added_by=SEED_USER_ID,
                )
                trip = _find_trip(db, year)
            if trip is None:
                continue
        else:
            # Compare the normalised country, or "germany" in the file reads as a
            # difference from the stored "Germany" on every single run.
            _apply_columns(
                db, "trips", trip, {**spec, "country": country}, TRIP_FIELDS,
                overwrite=overwrite, dry_run=dry_run, report=report, label=label,
            )

        claimed: set[int] = set()
        for match_spec in spec.get("matches", []):
            match = _find_match(db, trip["id"], match_spec, claimed)
            fixture = f"{match_spec.get('home', '?')} vs {match_spec.get('away', '?')}"
            match_label = f"{label} · {fixture}"
            created = False

            if match is None:
                if not create:
                    report.problem(f"{match_label}: no matching fixture recorded (use --create)")
                    continue
                report.change(f"{match_label}: creating fixture")
                if dry_run:
                    continue
                match_id = db.execute(
                    """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                                 home_goals, away_goals, city, stadium,
                                                 attendance, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trip["id"],
                        match_spec.get("match_date"),
                        match_spec.get("competition"),
                        (match_spec.get("home") or "?").strip(),
                        (match_spec.get("away") or "?").strip(),
                        match_spec.get("home_goals"),
                        match_spec.get("away_goals"),
                        match_spec.get("city"),
                        match_spec.get("stadium"),
                        match_spec.get("attendance"),
                        match_spec.get("notes"),
                    ),
                )
                match = db.query_one("SELECT * FROM trip_matches WHERE id=?", (match_id,))
                created = True
            if match is None:
                continue

            # Claim it so a later spec on the same trip cannot bind here too.
            claimed.add(match["id"])
            if not created:
                _apply_columns(
                    db, "trip_matches", match, match_spec, MATCH_FIELDS,
                    overwrite=overwrite, dry_run=dry_run, report=report, label=match_label,
                )
            if match_spec.get("goals"):
                _apply_goals(
                    db, match["id"], list(match_spec["goals"]),
                    overwrite=overwrite, dry_run=dry_run, report=report, label=match_label,
                )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="path to the JSON file")
    parser.add_argument(
        "--create", action="store_true", help="insert trips/fixtures that are absent"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="let the file win over the database"
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    if not args.seed.exists():
        print(f"No seed file at {args.seed}")
        return 2
    try:
        payload = json.loads(args.seed.read_text())
    except json.JSONDecodeError as exc:
        print(f"{args.seed} is not valid JSON: {exc}")
        return 2

    entries = payload.get("trips") or []
    if not entries:
        print(f"{args.seed} has no trips yet — nothing to apply.")
        return 0

    try:
        cfg = config.load(require_discord=False)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n{exc}")
        return 2

    db = Database(cfg.db_path)
    db.connect()
    try:
        report = apply_seed(
            db, payload, create=args.create, overwrite=args.overwrite, dry_run=args.dry_run
        )
    finally:
        db.close()

    print(f"{args.seed} → {cfg.db_path}")
    for line in report.lines:
        print(line)
    if report.problems:
        print("\nNeeds attention:")
        for problem in report.problems:
            print(f"  ! {problem}")
    verb = "would change" if args.dry_run else "changed"
    print(f"\n{report.changes} {verb}, {len(report.problems)} problem(s).")
    return 1 if report.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
