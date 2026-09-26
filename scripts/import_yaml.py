#!/usr/bin/env python3
"""Rebuild the database from the YAML the club keeps its history in.

    python scripts/import_yaml.py --into data/restored.sqlite3
    python scripts/import_yaml.py --into /tmp/check.sqlite3 --from /somewhere
    python scripts/import_yaml.py --into data/restored.sqlite3 --force

The other half of `scripts/export_yaml.py`, and the half that matters the day
the laptop dies. It is also what makes "these files hold enough to recreate the
database" a fact rather than a hope: `tests/test_yaml_roundtrip.py` exports,
imports and compares, so the claim is checked rather than asserted.

`--into` is required and is never the live database by default. A target that
already holds wines is refused unless you say `--force`, because restoring on
top of a real cellar would double every bottle in it.

The schema comes from `bot/db.py` — `Database.connect()` runs the same
migrations the bot and the web app do, so a restored database is current by
construction and nothing here restates a CREATE TABLE.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import Database, utcnow_iso  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / "data"

IMPORT_USER_ID = 0


def _read(folder: Path, name: str) -> dict:
    import yaml

    path = folder / f"{name}.yml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _at(entry: dict, key: str, fallback: str) -> str:
    """A timestamp the file records, or the moment of the restore."""
    value = entry.get(key)
    return value if isinstance(value, str) and value else fallback


def load_wines(db: Database, document: dict, now: str) -> dict[str, int]:
    counts = {"members": 0, "tastings": 0, "wines": 0, "ratings": 0}
    members: dict[str, int] = {}
    for entry in document.get("members") or []:
        db.execute(
            "INSERT INTO wine_members (name, discord_id, guest) VALUES (?,?,?)",
            (entry["name"], entry.get("discord_id"), 1 if entry.get("guest") else 0),
        )
        members[entry["name"]] = db.query_one(
            "SELECT id FROM wine_members WHERE name=?", (entry["name"],)
        )["id"]
        counts["members"] += 1

    for entry in document.get("tastings") or []:
        recorded = entry.get("recorded") or {}
        db.execute(
            """INSERT INTO tastings (key, year, month, theme, location, host, added_at)
               VALUES (?,?,?,?,?,?,?)""",
            (entry["key"], entry["year"], entry.get("month"), entry.get("theme"),
             entry.get("location"), entry.get("host"), _at(recorded, "tasting", now)),
        )
        tasting_id = db.query_one(
            "SELECT id FROM tastings WHERE key=?", (entry["key"],)
        )["id"]
        counts["tastings"] += 1

        # In file order, because that is the order they were poured and the
        # page reads it back off the row ids this loop is about to hand out.
        for wine in entry.get("wines") or []:
            wine_id = db.execute(
                """INSERT INTO wines (name, producer, vintage, country, region, grape,
                                      price_nok, bought_at, tasting_id, brought_by,
                                      added_by, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wine["name"], wine.get("producer"), wine.get("vintage"),
                 wine.get("country"), wine.get("region"), wine.get("grape"),
                 wine.get("price_nok"), wine.get("bought_at"), tasting_id,
                 wine.get("brought_by"), wine.get("added_by", IMPORT_USER_ID),
                 _at(wine, "recorded", _at(recorded, "wines", now))),
            )
            counts["wines"] += 1

            for who, given in (wine.get("scores") or {}).items():
                # `Andy: 88`, or the long form when there was more to say.
                detail = given if isinstance(given, dict) else {"score": given}
                if who not in members:
                    raise SystemExit(
                        f"{entry['key']}: {wine['name']!r} was scored by {who!r}, "
                        "who is not in this file's members"
                    )
                db.execute(
                    """INSERT INTO wine_ratings (wine_id, member_id, score, notes, rated_at)
                       VALUES (?,?,?,?,?)""",
                    (wine_id, members[who], detail["score"], detail.get("notes"),
                     _at(detail, "rated_at", _at(recorded, "ratings", now))),
                )
                counts["ratings"] += 1
    return counts


def load_trips(db: Database, document: dict, now: str) -> dict[str, int]:
    counts = {"trips": 0, "matches": 0, "goals": 0}
    for entry in document.get("trips") or []:
        db.execute(
            """INSERT INTO trips (year, country, city, date_from, date_to, notes,
                                  added_by, added_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (entry["year"], entry["country"], entry.get("city"), entry.get("date_from"),
             entry.get("date_to"), entry.get("notes"), entry.get("added_by", 0),
             _at(entry, "added_at", now)),
        )
        trip_id = db.query_one("SELECT id FROM trips WHERE year=?", (entry["year"],))["id"]
        counts["trips"] += 1

        for match in entry.get("matches") or []:
            match_id = db.execute(
                """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                             home_goals, away_goals, city, stadium,
                                             attendance, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (trip_id, match.get("match_date"), match.get("competition"),
                 match["home"], match["away"], match.get("home_goals"),
                 match.get("away_goals"), match.get("city"), match.get("stadium"),
                 match.get("attendance"), match.get("notes")),
            )
            counts["matches"] += 1
            for goal in match.get("goals") or []:
                db.execute(
                    """INSERT INTO trip_goals (trip_match_id, minute, scorer, side)
                       VALUES (?,?,?,?)""",
                    (match_id, goal.get("minute"), goal["scorer"], goal.get("side")),
                )
                counts["goals"] += 1
    return counts


def load_events(db: Database, document: dict, now: str) -> dict[str, int]:
    counts = {"events": 0, "bottles": 0}
    for entry in document.get("events") or []:
        tasting_id = trip_id = None
        if entry.get("tasting"):
            found = db.query_one("SELECT id FROM tastings WHERE key=?", (entry["tasting"],))
            tasting_id = found["id"] if found else None
        if entry.get("trip"):
            found = db.query_one("SELECT id FROM trips WHERE year=?", (entry["trip"],))
            trip_id = found["id"] if found else None
        event_id = db.execute(
            """INSERT INTO events (kind, theme, location, host, country, starts_at,
                                   ends_at, notes, tasting_id, trip_id,
                                   created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry.get("kind", "tasting"), entry["theme"], entry.get("location"),
             entry.get("host"), entry.get("country"), entry["starts_at"],
             entry.get("ends_at"), entry.get("notes"), tasting_id, trip_id,
             entry.get("created_by"), _at(entry, "created_at", now)),
        )
        counts["events"] += 1
        for seat, wine in enumerate(entry.get("wines") or [], start=1):
            db.execute(
                """INSERT INTO event_wines (event_id, name, producer, vintage, country,
                                            region, grape, price_nok, brought_by,
                                            position, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, wine["name"], wine.get("producer"), wine.get("vintage"),
                 wine.get("country"), wine.get("region"), wine.get("grape"),
                 wine.get("price_nok"), wine.get("brought_by"), seat,
                 _at(wine, "added_at", now)),
            )
            counts["bottles"] += 1
    return counts


def restore(db: Database, folder: Path) -> dict[str, int]:
    """Read the three files into an empty database, in dependency order."""
    now = utcnow_iso()
    counts: dict[str, int] = {}
    counts.update(load_wines(db, _read(folder, "wines"), now))
    counts.update(load_trips(db, _read(folder, "trips"), now))
    # Last, because an event names the tasting or the trip it became.
    counts.update(load_events(db, _read(folder, "events"), now))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--into", type=Path, required=True,
                        help="the database to build (never the live one by default)")
    parser.add_argument("--from", dest="folder", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--force", action="store_true",
                        help="write into a database that already holds wines")
    args = parser.parse_args(argv)

    if not args.folder.exists():
        print(f"No folder at {args.folder}")
        return 2

    db = Database(args.into)
    db.connect()
    standing = db.query_one("SELECT COUNT(*) n FROM wines")["n"]
    if standing and not args.force:
        db.close()
        print(f"{args.into} already holds {standing} wines. "
              "Restoring on top would double them — pass --force if you mean it.")
        return 2

    counts = restore(db, args.folder)
    db.close()
    print(f"restored into {args.into}")
    for name, n in counts.items():
        print(f"  {n:>5} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
