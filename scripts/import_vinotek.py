#!/usr/bin/env python3
"""Load the club's tasting spreadsheet into the wine database.

    python scripts/import_vinotek.py --dry-run    # report, write nothing
    python scripts/import_vinotek.py              # do it

Safe to re-run: tastings are keyed, and a wine already present has its ratings
updated rather than duplicated.

The workbook lives in data/, which is gitignored — it holds nine people's scores
and must not end up in the repo. All the parsing rules are in bot/vinotek.py.
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, vinotek  # noqa: E402
from bot.db import GUESTS, Database, utcnow_iso  # noqa: E402

DEFAULT_WORKBOOK = Path(__file__).resolve().parent.parent / "data" / "FC Vino Vinotek.xlsx"

# The three tastings whose Tid gives a month but no year. October, November and
# December are missing from 2021 and from no other year with data, and 2021's
# records stop dead after September.
MONTH_ONLY_YEAR = 2021

# Imported rows carry user id 0, which no Discord account can have, so it is
# obvious in the database that a row came from the spreadsheet.
IMPORT_USER_ID = 0


class Harvest:
    """Everything read out of the workbook, before anything is written."""

    def __init__(self) -> None:
        self.wines: list[tuple[str, vinotek.ParsedWine]] = []
        self.skips: collections.Counter = collections.Counter()
        self.notes: list[str] = []
        self.per_sheet: collections.Counter = collections.Counter()


def read_workbook(path: Path) -> Harvest:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    harvest = Harvest()

    for spec in vinotek.SHEETS:
        if spec.sheet not in workbook.sheetnames:
            harvest.notes.append(f"sheet {spec.sheet!r} is not in the workbook")
            continue
        rows = list(workbook[spec.sheet].iter_rows(values_only=True))
        if not rows:
            continue
        # A sheet can hold several tables side by side: 2016 has two.
        blocks = vinotek.locate_blocks(list(rows[0]))
        if len(blocks) > 1:
            harvest.notes.append(f"{spec.sheet}: {len(blocks)} tables side by side")
        for block in blocks:
            for row in rows[1:]:
                wine, reason = vinotek.parse_row(
                    row, block, spec, month_only_year=MONTH_ONLY_YEAR
                )
                if wine is None:
                    harvest.skips[f"{spec.sheet}: {reason}"] += 1
                    continue
                harvest.wines.append((spec.sheet, wine))
                harvest.per_sheet[spec.sheet] += 1
    return harvest


def repair_missing_periods(harvest: Harvest) -> int:
    """Give an undated row the date of its own tasting.

    One row has the wine name in the Tid column instead of a date. Its theme and
    location match rows that are dated, so the date comes from those rather than
    from a guess.
    """
    by_theme: dict[tuple, vinotek.Period] = {}
    for _, wine in harvest.wines:
        if wine.period and wine.theme:
            by_theme.setdefault((wine.theme.lower(), (wine.location or "").lower()), wine.period)

    repaired = 0
    for index, (sheet, wine) in enumerate(harvest.wines):
        if wine.period is not None or not wine.theme:
            continue
        found = by_theme.get((wine.theme.lower(), (wine.location or "").lower()))
        if found:
            harvest.wines[index] = (sheet, wine._replace(period=found))
            harvest.notes.append(
                f"dated {wine.name!r} as {found.year}-{found.month:02d} "
                f"from its theme {wine.theme!r}"
            )
            repaired += 1
    return repaired


def undo_fill_handle(harvest: Harvest) -> list[str]:
    """Repair evenings split apart by a dragged Excel cell. See bot/vinotek.py."""
    sheets = [sheet for sheet, _ in harvest.wines]
    repaired, notes = vinotek.undo_fill_handle([wine for _, wine in harvest.wines])
    harvest.wines[:] = list(zip(sheets, repaired, strict=True))
    harvest.notes.extend(notes)
    return notes


def drop_cross_sheet_duplicates(harvest: Harvest) -> int:
    """Remove rows recorded in two sheets.

    The January 2014 "Top of the pops" tasting appears in both the 2013 and 2014
    sheets. The copy that carries a month is kept, so the tasting lands on the
    calendar; a genuine re-tasting of the same bottle scores differently and so
    has a different signature.
    """
    best: dict[tuple, tuple[int, str, vinotek.ParsedWine]] = {}
    for index, (sheet, wine) in enumerate(harvest.wines):
        key = wine.signature
        rank = (wine.period is not None, bool(wine.period and wine.period.month))
        if key not in best:
            best[key] = (index, sheet, wine)
            continue
        _, other_sheet, other = best[key]
        other_rank = (other.period is not None, bool(other.period and other.period.month))
        if rank > other_rank:
            harvest.notes.append(
                f"dropped {other.name!r} from {other_sheet} — "
                f"same wine and scores in {sheet}, dated"
            )
            best[key] = (index, sheet, wine)
        else:
            harvest.notes.append(
                f"dropped {wine.name!r} from {sheet} — same wine and scores in {other_sheet}, dated"
            )

    dropped = len(harvest.wines) - len(best)
    harvest.wines = [(sheet, wine) for _, sheet, wine in sorted(best.values())]
    return dropped


def check_against_sheet(harvest: Harvest) -> dict[str, tuple[int, int, str]]:
    """Check the scores read against the sheet's own arithmetic.

    Prefers the `Sum` column, which is an unambiguous statement of "these are
    the numbers in these cells". `Poeng` is a worse oracle: it is a plain mean
    in Alltime, a trimmed mean in 2013–2015 (drop the highest and lowest), and
    something else again in 2016 — so a disagreement there says nothing about
    whether the right cells were read.
    """
    result: dict[str, list] = collections.defaultdict(lambda: [0, 0, "—"])
    for sheet, wine in harvest.wines:
        if not wine.scores:
            continue
        scale = next(s.scale for s in vinotek.SHEETS if s.sheet == sheet)
        counts = result[sheet]
        # Scores are rounded to integers, so each can drift half a point.
        slack = 0.5 * len(wine.scores) + 0.01
        if wine.stated_total is not None:
            counts[2] = "Sum"
            counts[1] += 1
            if abs(wine.stated_total * scale - sum(wine.scores.values())) > slack:
                counts[0] += 1
        elif wine.stated_average is not None:
            counts[2] = "Poeng (plain mean)"
            counts[1] += 1
            if abs(wine.stated_average * scale - statistics.fmean(wine.scores.values())) > 0.75:
                counts[0] += 1
    return {sheet: (bad, total, oracle) for sheet, (bad, total, oracle) in result.items()}


def group_into_tastings(harvest: Harvest) -> dict[str, dict]:
    """Collect the wines of each evening and work out what varies within it.

    `Ansvarlig` and `Sted` swap meaning between years. On a normal night both
    describe the evening — who ran it and whose living room. On a bring-your-own
    night one of them varies row by row, because it has been used to record who
    brought that particular bottle: in December 2021 that was `Ansvarlig`, in
    December 2023 it was `Sted`.

    So: a column that holds one value across the evening describes the evening,
    and a column that varies describes the wine.
    """
    groups: dict[str, dict] = {}
    for _, wine in harvest.wines:
        key = vinotek.tasting_key(wine.period, wine.theme)
        group = groups.setdefault(
            key, {"period": wine.period, "theme": wine.theme, "wines": []}
        )
        group["wines"].append(wine)

    for key, group in groups.items():
        wines = group["wines"]
        hosts = {w.brought_by for w in wines if w.brought_by}
        places = {w.location for w in wines if w.location}
        group["host"] = next(iter(hosts)) if len(hosts) == 1 else None
        group["location"] = next(iter(places)) if len(places) == 1 else None
        # Whichever column varies is per-wine information.
        if len(hosts) > 1:
            group["per_wine"] = "brought_by"
        elif len(places) > 1:
            group["per_wine"] = "location"
        else:
            group["per_wine"] = None
        if group["per_wine"]:
            harvest.notes.append(
                f"{key}: {group['per_wine']} varies across {len(wines)} wines — "
                "treating it as who brought each bottle"
            )
    return groups


def write(db: Database, harvest: Harvest, *, dry_run: bool, reset: bool = False) -> dict[str, int]:
    counts = collections.Counter()

    if reset and not dry_run:
        for table in ("wine_ratings", "wines", "tastings"):
            db.execute(f"DELETE FROM {table}")
        harvest.notes.append("cleared tastings, wines and wine_ratings before importing")

    members: dict[str, int] = {}
    for name in vinotek.MEMBERS:
        if not dry_run:
            # The sheet records who scored what and nothing about membership,
            # so guests would arrive looking like everybody else.
            db.execute(
                "INSERT INTO wine_members (name, guest) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET guest=excluded.guest",
                (name, 1 if name in GUESTS else 0),
            )
            members[name] = db.query_one("SELECT id FROM wine_members WHERE name=?", (name,))["id"]
        counts["members"] += 1

    for key, group in group_into_tastings(harvest).items():
        period = group["period"]
        counts["tastings"] += 1
        if dry_run:
            tasting_id = -1
        else:
            db.execute(
                """INSERT INTO tastings (key, year, month, theme, location, host, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET
                       location=excluded.location, host=excluded.host""",
                (
                    key,
                    period.year if period else 0,
                    period.month if period else None,
                    group["theme"],
                    group["location"],
                    group["host"],
                    utcnow_iso(),
                ),
            )
            tasting_id = db.query_one("SELECT id FROM tastings WHERE key=?", (key,))["id"]

        seen: set[str] = set()
        for wine in group["wines"]:
            if group["per_wine"] == "location":
                brought_by = wine.location
            elif group["per_wine"] == "brought_by":
                brought_by = wine.brought_by
            else:
                brought_by = None

            if wine.name.lower() in seen:
                harvest.notes.append(f"{key}: two rows named {wine.name!r}, merged")
            seen.add(wine.name.lower())

            if dry_run:
                counts["wines"] += 1
                counts["ratings"] += len(wine.scores)
                continue

            existing = db.query_one(
                "SELECT id FROM wines WHERE tasting_id=? AND LOWER(name)=LOWER(?)",
                (tasting_id, wine.name),
            )
            if existing:
                wine_id = existing["id"]
                db.execute(
                    """UPDATE wines SET price_nok=COALESCE(?, price_nok),
                                        brought_by=COALESCE(?, brought_by) WHERE id=?""",
                    (wine.price_nok, brought_by, wine_id),
                )
            else:
                wine_id = db.execute(
                    """INSERT INTO wines (name, price_nok, tasting_id, brought_by,
                                          added_by, added_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        wine.name,
                        wine.price_nok,
                        tasting_id,
                        brought_by,
                        IMPORT_USER_ID,
                        utcnow_iso(),
                    ),
                )
                counts["wines"] += 1

            for member, score in wine.scores.items():
                db.execute(
                    """INSERT INTO wine_ratings (wine_id, member_id, score, rated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(wine_id, member_id) DO UPDATE SET score=excluded.score""",
                    (wine_id, members[member], score, utcnow_iso()),
                )
                counts["ratings"] += 1
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--verbose", action="store_true", help="print every note")
    parser.add_argument(
        "--reset", action="store_true",
        help="clear tastings, wines and ratings first — use after changing a parsing rule",
    )
    args = parser.parse_args(argv)

    if not args.workbook.exists():
        print(f"No workbook at {args.workbook}")
        return 2

    harvest = read_workbook(args.workbook)
    print(f"{args.workbook.name}: {len(harvest.wines)} scored rows read")
    for sheet, n in harvest.per_sheet.items():
        print(f"    {sheet:>8}: {n}")

    repaired = repair_missing_periods(harvest)
    dropped = drop_cross_sheet_duplicates(harvest)
    unfilled = undo_fill_handle(harvest)
    print(f"\n  repaired dates: {repaired}   cross-sheet duplicates dropped: {dropped}")
    if unfilled:
        print("\n  evenings put back together after an Excel fill-handle drag:")
        for note in unfilled:
            print(f"    {note}")

    print("\n  skipped rows:")
    for reason, n in sorted(harvest.skips.items()):
        print(f"    {reason}: {n}")

    print("\n  scores read vs the sheet's own arithmetic:")
    for sheet, (bad, total, oracle) in check_against_sheet(harvest).items():
        verdict = "✓" if bad == 0 else f"⚠️ {bad} disagree"
        print(f"    {sheet:>8}: {total - bad}/{total} via {oracle:<18} {verdict}")

    undated = sum(1 for _, w in harvest.wines if w.period is None)
    no_month = sum(1 for _, w in harvest.wines if w.period and w.period.month is None)
    print(f"\n  rows with no date at all: {undated}   year but no month: {no_month}")

    try:
        cfg = config.load(require_discord=False)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n{exc}")
        return 2

    db = Database(cfg.db_path)
    db.connect()
    try:
        counts = write(db, harvest, dry_run=args.dry_run, reset=args.reset)
    finally:
        db.close()

    if args.verbose or len(harvest.notes) <= 15:
        for note in harvest.notes:
            print(f"    · {note}")
    else:
        print(f"\n  {len(harvest.notes)} notes ( --verbose to see them all):")
        for note in harvest.notes[:10]:
            print(f"    · {note}")

    if args.dry_run:
        print(
            f"\nwould write: {counts.get('tastings', 0)} tastings, "
            f"{counts.get('wines', 0)} wines, {counts.get('ratings', 0)} ratings"
        )
        return 0

    # Report what the database actually holds rather than how many statements
    # ran: an upsert that changes nothing still counts as a statement.
    db = Database(cfg.db_path)
    db.connect()
    try:
        totals = {
            table: db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("tastings", "wines", "wine_ratings", "wine_members")
        }
    finally:
        db.close()
    print(
        f"\ndatabase now holds: {totals['tastings']} tastings, {totals['wines']} wines, "
        f"{totals['wine_ratings']} ratings, {totals['wine_members']} members"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
