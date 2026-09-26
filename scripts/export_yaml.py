#!/usr/bin/env python3
"""Write the club's own history out as YAML, so it can live somewhere safe.

    python scripts/export_yaml.py                    # all three, into data/
    python scripts/export_yaml.py --only trips
    python scripts/export_yaml.py --to /somewhere/else

The database is one SQLite file on one laptop, and `data/` is gitignored
because this repository is public — so nine people's history has nowhere it can
be kept. These files give it one: a private repository, where the file's own
git history becomes the provenance and a diff reads like a sentence.

Three files, because the diary straddles the other two:

    wines.yml   the members, the evenings, the bottles, every score
    trips.yml   the away trips, their matches and their goals
    events.yml  the diary, and which tasting or trip each event became

Not exported, deliberately: matches, predictions, prediction_scores, members,
reminders_sent, announcements, bot_messages, guild_channels. Those are the
football API's cache and Discord's plumbing — rebuilt from the API and from the
server, never authored by anyone.

`scripts/import_yaml.py` reads these back. The two are a pair; changing the
shape here means changing it there, and `tests/test_yaml_roundtrip.py` is what
notices when only one of them was changed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.db import Database  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / "data"

FILES = ("wines", "trips", "events")

# The sentinel every imported row carries, and the only value in the column, so
# writing it 1172 times would say nothing.
IMPORT_USER_ID = 0

HEADERS = {
    "wines": """\
# FC Vino — the cellar.
#
# The members, every evening, every bottle and every score. Written by
# scripts/export_yaml.py and read back by scripts/import_yaml.py, which can
# rebuild the database from this file alone.
#
# An evening is identified by its `key`, a bottle by its name within that
# evening (and who brought it, when two people brought the same wine), a score
# by the member's name. No row ids appear anywhere, so a rebuild may renumber
# everything and this file will not change by a line.
#
# The order of `wines:` within an evening is the order they were poured.
# A key that is absent was NULL. `recorded:` is bookkeeping — when the rows
# were written, not when the wine was drunk.
""",
    "trips": """\
# FC Vino — the away trips.
#
# One trip a year, the matches we saw and who scored. Written by
# scripts/export_yaml.py and read back by scripts/import_yaml.py.
#
# A trip is identified by its year, which is unique in the database too. A key
# that is absent was NULL.
""",
    "events": """\
# FC Vino — the diary.
#
# Evenings registered but not yet held, and the ones that have been filed. An
# event that became a tasting or a trip names it by key or by year rather than
# by id, so the diary survives a rebuild that renumbers everything.
#
# Written by scripts/export_yaml.py and read back by scripts/import_yaml.py.
""",
}


def _dump(document: dict, header: str) -> str:
    import yaml

    class Indented(yaml.SafeDumper):
        """Indent a list under its key, which PyYAML otherwise declines to do.

        Both are valid YAML and only one is readable at this depth: a bottle's
        fields are four levels in, and without this its dash sits under the
        `wines:` it belongs to rather than inside it.
        """

        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    return header + yaml.dump(
        document,
        Dumper=Indented,
        sort_keys=False,        # our order is meaning: pour order, chronology
        allow_unicode=True,     # Håvard and Côte-Rôtie should read as themselves
        default_flow_style=False,
        width=100000,           # never wrap mid-value; a wrapped name is a bad diff
    )


def _fields(row, names: tuple[str, ...]) -> dict[str, Any]:
    """The columns that have something in them. A null is absent, not `null`."""
    return {name: row[name] for name in names if row[name] is not None}


# -- the cellar -------------------------------------------------------------

TASTING_FIELDS = ("year", "month", "theme", "location", "host")
WINE_FIELDS = ("producer", "vintage", "country", "region", "grape",
               "price_nok", "bought_at", "brought_by")


def _scores(db: Database, wine_id: int, common: str | None) -> dict[str, Any]:
    """Every score on a bottle, by member name.

    `Andy: 88` unless there is more to say — a note, or a rating written at a
    different moment from the rest of the evening — in which case the long form
    carries it. Sorted by name, because their order means nothing and a stable
    file means everything.
    """
    scores: dict[str, Any] = {}
    for row in db.query(
        """SELECT m.name, r.score, r.notes, r.rated_at
           FROM wine_ratings r JOIN wine_members m ON m.id = r.member_id
           WHERE r.wine_id = ? ORDER BY m.name""",
        (wine_id,),
    ):
        extra = {}
        if row["notes"]:
            extra["notes"] = row["notes"]
        if row["rated_at"] != common:
            extra["rated_at"] = row["rated_at"]
        scores[row["name"]] = {"score": row["score"], **extra} if extra else row["score"]
    return scores


def _common(values: list[str]) -> str | None:
    """The one timestamp they all share, or None when they do not share one."""
    unique = set(values)
    return values[0] if len(unique) == 1 else None


def export_wines(db: Database) -> dict:
    members = []
    for row in db.query("SELECT name, discord_id, guest FROM wine_members ORDER BY name"):
        member: dict[str, Any] = {"name": row["name"]}
        if row["discord_id"] is not None:
            member["discord_id"] = row["discord_id"]
        if row["guest"]:
            member["guest"] = True
        members.append(member)

    tastings = []
    for row in db.query(
        """SELECT id, key, year, month, theme, location, host, added_at
           FROM tastings ORDER BY year, IFNULL(month, 0), key"""
    ):
        # Row-id order, which is the order they were poured — see
        # web/queries.tasting_wines, which now falls back to exactly this.
        wines = db.query(
            "SELECT * FROM wines WHERE tasting_id = ? ORDER BY id", (row["id"],)
        )
        rated = db.query(
            """SELECT r.rated_at FROM wine_ratings r JOIN wines w ON w.id = r.wine_id
               WHERE w.tasting_id = ?""",
            (row["id"],),
        )
        # Hoisted, because an import writes an evening in one pass; a row that
        # disagrees says so itself rather than forcing every row to repeat it.
        wines_at = _common([w["added_at"] for w in wines])
        ratings_at = _common([r["rated_at"] for r in rated])
        recorded = {"tasting": row["added_at"]}
        if wines_at:
            recorded["wines"] = wines_at
        if ratings_at:
            recorded["ratings"] = ratings_at

        entry: dict[str, Any] = {"key": row["key"], **_fields(row, TASTING_FIELDS)}
        entry["recorded"] = recorded
        entry["wines"] = [_wine(db, w, wines_at, ratings_at) for w in wines]
        tastings.append(entry)

    return {"members": members, "tastings": tastings}


def _wine(db: Database, row, wines_at: str | None, ratings_at: str | None) -> dict:
    wine: dict[str, Any] = {"name": row["name"], **_fields(row, WINE_FIELDS)}
    if row["added_by"] != IMPORT_USER_ID:
        wine["added_by"] = row["added_by"]
    if row["added_at"] != wines_at:
        wine["recorded"] = row["added_at"]
    scores = _scores(db, row["id"], ratings_at)
    if scores:
        wine["scores"] = scores
    return wine


# -- the away trips ---------------------------------------------------------

TRIP_FIELDS = ("country", "city", "date_from", "date_to", "notes")
MATCH_FIELDS = ("match_date", "competition", "home", "away", "home_goals",
                "away_goals", "city", "stadium", "attendance", "notes")
GOAL_FIELDS = ("minute", "scorer", "side")


def export_trips(db: Database) -> dict:
    trips = []
    for row in db.query("SELECT * FROM trips ORDER BY year"):
        trip: dict[str, Any] = {"year": row["year"], **_fields(row, TRIP_FIELDS)}
        trip["added_by"] = row["added_by"]
        trip["added_at"] = row["added_at"]
        matches = []
        for played in db.query(
            "SELECT * FROM trip_matches WHERE trip_id = ? ORDER BY match_date, id",
            (row["id"],),
        ):
            match: dict[str, Any] = _fields(played, MATCH_FIELDS)
            goals = [
                _fields(goal, GOAL_FIELDS)
                for goal in db.query(
                    "SELECT * FROM trip_goals WHERE trip_match_id = ? ORDER BY minute, id",
                    (played["id"],),
                )
            ]
            if goals:
                match["goals"] = goals
            matches.append(match)
        if matches:
            trip["matches"] = matches
        trips.append(trip)
    return {"trips": trips}


# -- the diary --------------------------------------------------------------

EVENT_FIELDS = ("kind", "theme", "location", "host", "country", "starts_at",
                "ends_at", "notes", "created_by")
EVENT_WINE_FIELDS = ("producer", "vintage", "country", "region", "grape",
                     "price_nok", "brought_by")


def export_events(db: Database) -> dict:
    events = []
    for row in db.query("SELECT * FROM events ORDER BY starts_at, id"):
        event: dict[str, Any] = _fields(row, EVENT_FIELDS)
        event["created_at"] = row["created_at"]
        # By key and by year, never by id: a rebuild renumbers everything, and
        # a diary that pointed at id 966 would afterwards point at a stranger.
        if row["tasting_id"] is not None:
            found = db.query_one("SELECT key FROM tastings WHERE id = ?", (row["tasting_id"],))
            if found:
                event["tasting"] = found["key"]
        if row["trip_id"] is not None:
            found = db.query_one("SELECT year FROM trips WHERE id = ?", (row["trip_id"],))
            if found:
                event["trip"] = found["year"]
        wines = []
        for wine in db.query(
            "SELECT * FROM event_wines WHERE event_id = ? ORDER BY position, id",
            (row["id"],),
        ):
            lined_up: dict[str, Any] = {"name": wine["name"], **_fields(wine, EVENT_WINE_FIELDS)}
            lined_up["added_at"] = wine["added_at"]
            wines.append(lined_up)
        if wines:
            event["wines"] = wines
        events.append(event)
    return {"events": events}


EXPORTERS = {"wines": export_wines, "trips": export_trips, "events": export_events}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", type=Path, default=DEFAULT_DIR, help="where to write")
    parser.add_argument("--only", choices=FILES, help="write one of the three")
    args = parser.parse_args(argv)

    cfg = config.load(require_discord=False)
    db = Database(cfg.db_path)
    db.connect()
    args.to.mkdir(parents=True, exist_ok=True)

    for name in ([args.only] if args.only else FILES):
        document = EXPORTERS[name](db)
        path = args.to / f"{name}.yml"
        path.write_text(_dump(document, HEADERS[name]), encoding="utf-8")
        counted = ", ".join(f"{len(rows)} {kind}" for kind, rows in document.items())
        print(f"{path}  {counted}, {path.stat().st_size / 1024:.0f} kB")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
