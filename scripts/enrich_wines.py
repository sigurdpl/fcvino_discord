#!/usr/bin/env python3
"""Fill in country, region, grape and vintage for the imported wines.

    python scripts/enrich_wines.py --dry-run     # report, write nothing
    python scripts/enrich_wines.py               # do it
    python scripts/enrich_wines.py --overwrite   # redo rows already filled in

Two sources, in order:

1. `bot/wine_origin.py` — a reference table of appellations, grapes and country
   names. Across classical Europe the appellation *is* the grape, so most of
   this is tabulated fact rather than inference.
2. `seeds/wine_origins.json` — wines the table can't place, looked up by hand
   with a source recorded beside each. The more specific statement, so it wins.

Whatever is still empty afterwards is printed: that list is the worklist for the
next round of lookups.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, wine_origin  # noqa: E402
from bot.db import Database  # noqa: E402

SEED = Path(__file__).resolve().parent.parent / "seeds" / "wine_origins.json"

FIELDS = ("country", "region", "grape", "vintage")


def load_seed(path: Path) -> dict[str, dict]:
    """Researched origins, keyed on the folded wine name so spelling drifts don't matter."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {
        wine_origin.fold(entry["name"]).strip(): entry
        for entry in payload.get("wines", [])
        if entry.get("name")
    }


def resolve(row, seed: dict[str, dict]) -> dict[str, object]:
    """What this wine's country, region, grape and vintage should be."""
    derived = wine_origin.derive(row["name"], row["theme"])
    values: dict[str, object] = {
        "country": derived.country,
        "region": derived.region,
        "grape": derived.grape,
        "vintage": wine_origin.parse_vintage(row["name"]),
    }

    # The researched file is a deliberate statement about this exact wine, so it
    # overrides anything the table guessed from a substring.
    entry = seed.get(wine_origin.fold(row["name"]).strip())
    if entry:
        for field in ("country", "region", "grape", "vintage"):
            if entry.get(field) is not None:
                values[field] = entry[field]
        if entry.get("non_wine"):
            values["grape"] = None
    if wine_origin.is_non_wine(row["name"]):
        values["grape"] = None
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--overwrite", action="store_true", help="replace values already set")
    parser.add_argument("--seed", type=Path, default=SEED)
    parser.add_argument("--worklist", type=int, default=25, help="how many gaps to print")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(require_discord=False)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n{exc}")
        return 2

    seed = load_seed(args.seed)
    print(f"{len(seed)} wine(s) in {args.seed.name}")

    db = Database(cfg.db_path)
    db.connect()
    try:
        rows = db.query(
            """SELECT w.id, w.name, w.country, w.region, w.grape, w.vintage,
                      COALESCE(t.theme, '') AS theme, COUNT(r.score) AS ratings
               FROM wines w
               LEFT JOIN tastings t ON t.id = w.tasting_id
               LEFT JOIN wine_ratings r ON r.wine_id = w.id
               GROUP BY w.id ORDER BY w.name"""
        )
        changed = collections.Counter()
        filled = collections.Counter()
        gaps: list[tuple[int, str, str]] = []
        seed_hits = 0

        for row in rows:
            values = resolve(row, seed)
            if wine_origin.fold(row["name"]).strip() in seed:
                seed_hits += 1

            updates = {
                field: value
                for field, value in values.items()
                if value is not None and (args.overwrite or row[field] is None)
            }
            if updates and not args.dry_run:
                assignments = ", ".join(f"{field}=?" for field in updates)
                db.execute(
                    f"UPDATE wines SET {assignments} WHERE id=?",
                    (*updates.values(), row["id"]),
                )
            for field in updates:
                changed[field] += 1
            for field in FIELDS:
                if values[field] is not None or row[field] is not None:
                    filled[field] += 1
            if values["country"] is None and values["grape"] is None:
                gaps.append((row["ratings"], row["name"], row["theme"]))

        total = len(rows)
        print(f"\n{total} wines")
        for field in FIELDS:
            print(f"  {field:<8} {filled[field]:>5} known ({filled[field] / total:.0%})"
                  f"   {changed[field]:>4} {'would be ' if args.dry_run else ''}written")
        print(f"  matched in the researched file: {seed_hits}")

        print(f"\n{len(gaps)} wines have neither a country nor a grape.")
        if gaps:
            print("  most-tasted first — this is the worklist:")
            for ratings, name, theme in sorted(gaps, reverse=True)[: args.worklist]:
                print(f"    {ratings:>2} ratings  {name[:56]:<58} {theme[:24]}")
            if len(gaps) > args.worklist:
                print(f"    … and {len(gaps) - args.worklist} more")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
