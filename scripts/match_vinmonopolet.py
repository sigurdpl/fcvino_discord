#!/usr/bin/env python3
"""Find each bottle in Vinmonopolet's catalogue, for its label picture.

    python scripts/match_vinmonopolet.py --dry-run --limit 40   # look first
    python scripts/match_vinmonopolet.py                        # do it
    python scripts/match_vinmonopolet.py --again                # retry the misses

The club buys at Polet, so their catalogue and this cellar overlap — which is
what makes label photographs possible without paying anybody. Their search and
their image server are both open, so nothing here needs a key.

What it stores is the **product number** and nothing else. Their image URL is
built from it (`bot.vinmonopolet.Match.image`), so keeping the URL as well
would be a second thing to hold in step with the first.

Safe to re-run: a bottle that already has a code is skipped, so a run that dies
halfway costs only what it had not reached. `--again` retries the ones that
found nothing, which is worth doing after Polet's range changes — though a wine
they have stopped selling will never match, and a good few of the club's
fifteen years are wines nobody can buy today.

**It asks a shop's website about a thousand bottles.** One request a second,
with a user agent that says who is asking. Do not make it faster.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, vinmonopolet  # noqa: E402
from bot.db import Database  # noqa: E402


def wines_to_ask(db: Database, *, again: bool, limit: int | None) -> list:
    """Bottles with no picture yet, oldest first so a partial run is coherent."""
    where = "vmp_code IS NULL" if not again else "IFNULL(vmp_code, '') = ''"
    rows = db.query(f"SELECT id, name FROM wines WHERE {where} ORDER BY id")
    return rows[:limit] if limit else rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--limit", type=int, help="only the first N bottles")
    parser.add_argument("--again", action="store_true",
                        help="retry the ones that found nothing last time")
    parser.add_argument("--floor", type=float, default=0.6,
                        help="how much of our name theirs must account for")
    args = parser.parse_args(argv)

    cfg = config.load(require_discord=False)
    db = Database(cfg.db_path)
    db.connect()

    asking = wines_to_ask(db, again=args.again, limit=args.limit)
    total = db.query_one("SELECT COUNT(*) n FROM wines")["n"]
    known = db.query_one("SELECT COUNT(*) n FROM wines WHERE IFNULL(vmp_code,'') <> ''")["n"]
    print(f"{len(asking)} bottle(s) to ask about, of {total} in the cellar "
          f"({known} already matched)\n")

    found = missed = 0
    for wine in asking:
        match = vinmonopolet.best_match(wine["name"], floor=args.floor)
        if match is None:
            missed += 1
            print(f"  ·  {wine['name'][:46]:46} nothing")
        else:
            found += 1
            # Their vintage is usually not ours — they sell what they have.
            year = f" [{match.vintage}]" if match.vintage else ""
            print(f"  ✓  {wine['name'][:46]:46} {match.name[:34]!r}{year} "
                  f"{match.code} ({match.score:.2f})")
            if not args.dry_run:
                db.execute("UPDATE wines SET vmp_code=? WHERE id=?",
                           (match.code, wine["id"]))
        # Their website, not ours. One a second, whatever the answer was.
        time.sleep(vinmonopolet.PAUSE)

    db.close()
    verb = "would match" if args.dry_run else "matched"
    print(f"\n{verb}: {found}, nothing found for {missed}"
          + (f"  ({found / len(asking):.0%})" if asking else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
