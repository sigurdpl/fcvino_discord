"""SQLite storage.

Stdlib sqlite3 on purpose: for nine people every query here is sub-millisecond,
so an async driver would add a dependency and buy nothing. Timestamps are stored
as UTC ISO-8601 text so the file stays readable with any sqlite client.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 6

SCHEMA = """
-- `brought_by` is per wine, not per tasting: at a bring-your-own night every
-- bottle names a different person, which is exactly the thing worth keeping.
CREATE TABLE IF NOT EXISTS wines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    producer    TEXT,
    vintage     INTEGER,
    country     TEXT,
    region      TEXT,
    grape       TEXT,
    price_nok   INTEGER,
    bought_at   TEXT,
    tasting_id  INTEGER REFERENCES tastings(id) ON DELETE SET NULL,
    brought_by  TEXT,
    added_by    INTEGER NOT NULL,
    added_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wines_name ON wines(name);

-- Who tasted what. Keyed on wine_members rather than a Discord id, because the
-- club's history comes from a spreadsheet that knows people by first name and
-- has no idea what Discord is. A member gets a discord_id when they claim their
-- name with /wine iam.
CREATE TABLE IF NOT EXISTS wine_ratings (
    wine_id   INTEGER NOT NULL REFERENCES wines(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES wine_members(id) ON DELETE CASCADE,
    score     INTEGER NOT NULL CHECK (score BETWEEN 1 AND 100),
    notes     TEXT,
    rated_at  TEXT    NOT NULL,
    PRIMARY KEY (wine_id, member_id)
);

CREATE TABLE IF NOT EXISTS wine_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    discord_id INTEGER UNIQUE
);

-- One evening: a theme, a host's living room, and the wines opened there. The
-- spreadsheet's Tid/Tema/Sted columns had nowhere to live before this.
--
-- `key` is a deterministic "2021-12|BYO|Lennart" and is what makes re-running
-- the import idempotent. It exists because SQLite treats NULLs as distinct in a
-- UNIQUE index, so UNIQUE(year, month, theme, location) would cheerfully insert
-- 2013's month-less tastings a second time. `month` is null when the sheet only
-- gave us a year.
CREATE TABLE IF NOT EXISTS tastings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT    NOT NULL UNIQUE,
    year     INTEGER NOT NULL,
    month    INTEGER,
    theme    TEXT,
    location TEXT,
    host     TEXT,
    added_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tastings_when ON tastings(year, month);

-- An evening we have not held yet. Deliberately not a row in `tastings`: that
-- table knows a year and a month and no day or time, its unique key would
-- collide for two evenings a month apart on the same theme, and — the reason
-- that decides it — `tastings` is counted everywhere as "evenings we held". A
-- planned one in there would quietly make the cellar's headline numbers wrong.
--
-- `starts_at` is UTC, like every other instant here; the web app converts to
-- Europe/Oslo for display. `tasting_id` is null until the evening is promoted
-- into the archive, and stamped afterwards so it cannot be promoted twice.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    theme      TEXT    NOT NULL,
    location   TEXT,
    host       TEXT,
    starts_at  TEXT    NOT NULL,
    notes      TEXT,
    tasting_id INTEGER REFERENCES tastings(id) ON DELETE SET NULL,
    created_by TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_when ON events(starts_at);

-- The bottles lined up for an evening. The columns mirror `wines` so that
-- promoting an event is a copy rather than a translation, and so "the details
-- we keep" means the same thing before and after the cork comes out.
CREATE TABLE IF NOT EXISTS event_wines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    producer   TEXT,
    vintage    INTEGER,
    country    TEXT,
    region     TEXT,
    grape      TEXT,
    price_nok  INTEGER,
    brought_by TEXT,
    position   INTEGER,
    added_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_wines_event ON event_wines(event_id);

CREATE TABLE IF NOT EXISTS members (
    user_id            INTEGER PRIMARY KEY,
    favourite_team_id  INTEGER,
    favourite_team_name TEXT,
    updated_at         TEXT
);

-- Local mirror of football-data.org, so reminders, predictions and scoring
-- never depend on a live API call succeeding at exactly the right moment.
CREATE TABLE IF NOT EXISTS matches (
    match_id    INTEGER PRIMARY KEY,
    competition TEXT    NOT NULL,
    matchday    INTEGER,
    home_id     INTEGER,
    home        TEXT    NOT NULL,
    away_id     INTEGER,
    away        TEXT    NOT NULL,
    kickoff_utc TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    home_goals  INTEGER,
    away_goals  INTEGER,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_kickoff ON matches(kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_matches_comp_md ON matches(competition, matchday);

CREATE TABLE IF NOT EXISTS predictions (
    match_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    home_goals INTEGER NOT NULL,
    away_goals INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (match_id, user_id)
);

CREATE TABLE IF NOT EXISTS prediction_scores (
    match_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    points    INTEGER NOT NULL,
    scored_at TEXT    NOT NULL,
    PRIMARY KEY (match_id, user_id)
);

-- Idempotency guards: a restart must not re-ping or double-score.
CREATE TABLE IF NOT EXISTS reminders_sent (
    match_id INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    sent_at  TEXT    NOT NULL,
    PRIMARY KEY (match_id, kind)
);

CREATE TABLE IF NOT EXISTS announcements (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- The away-trips archive: one place a year since 2010, sometimes more than one
-- match while we are there. Hand-entered, because the football API's free tier
-- has no data before the 2023/24 season. Dates here are calendar days
-- (YYYY-MM-DD), not the UTC kickoff instants `matches` stores.
--
-- The year alone is unique: we go one place a year, so re-adding a year amends
-- that trip rather than creating a second one.
CREATE TABLE IF NOT EXISTS trips (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    year      INTEGER NOT NULL UNIQUE,
    country   TEXT    NOT NULL,
    city      TEXT,
    date_from TEXT,
    date_to   TEXT,
    notes     TEXT,
    added_by  INTEGER NOT NULL,
    added_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_year ON trips(year);

-- `city` is per match and falls back to the trip's: a London trip needs no
-- per-match city, a Ruhr trip taking in Dortmund and Gelsenkirchen does.
CREATE TABLE IF NOT EXISTS trip_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    match_date  TEXT,
    competition TEXT,
    home        TEXT    NOT NULL,
    away        TEXT    NOT NULL,
    home_goals  INTEGER,
    away_goals  INTEGER,
    city        TEXT,
    stadium     TEXT,
    attendance  INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trip_matches_trip ON trip_matches(trip_id);

CREATE TABLE IF NOT EXISTS trip_goals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_match_id INTEGER NOT NULL REFERENCES trip_matches(id) ON DELETE CASCADE,
    minute        INTEGER,
    scorer        TEXT    NOT NULL,
    side          TEXT    CHECK (side IN ('H', 'A'))
);
CREATE INDEX IF NOT EXISTS idx_trip_goals_match ON trip_goals(trip_match_id);

-- One row per (guild, kind) rather than a column per kind, so a new kind of
-- automatic post costs a row and not an ALTER TABLE. Replaces guild_config,
-- which `_migrate_guild_config` drains and drops.
CREATE TABLE IF NOT EXISTS guild_channels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);

-- Messages the bot keeps up to date in place, e.g. the league table. Holding
-- the message id means a restart carries on editing the same message instead of
-- posting a second one, and content_hash means a table that hasn't moved is
-- left alone rather than re-edited every half hour.
CREATE TABLE IF NOT EXISTS bot_messages (
    guild_id     INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    content_hash TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, kind)
);
"""

CHANNEL_KINDS = ("football", "wine", "predictions", "standings", "trips")


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from the DB or the API into aware UTC."""
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so a stray asyncio.to_thread call can't trip us up.
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        self.migrate()
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def migrate(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.executescript(SCHEMA)
        self._migrate_guild_config()
        self._migrate_trip_match_city()
        self._migrate_trip_year_key()
        self._migrate_tasting_host()
        self._migrate_wine_columns()
        self._migrate_wine_ratings_members()
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def _migrate_tasting_host(self) -> None:
        """Add tastings.host to a database created before it existed."""
        conn = self._conn
        assert conn is not None
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tastings)")}
        if columns and "host" not in columns:
            conn.execute("ALTER TABLE tastings ADD COLUMN host TEXT")
            conn.commit()
            log.info("added tastings.host")

    def _migrate_wine_columns(self) -> None:
        """Add wines.tasting_id and wines.brought_by where they're missing."""
        conn = self._conn
        assert conn is not None
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(wines)")}
        if not columns:
            return
        for column, ddl in (
            (
                "tasting_id",
                "ALTER TABLE wines ADD COLUMN tasting_id INTEGER REFERENCES tastings(id)",
            ),
            ("brought_by", "ALTER TABLE wines ADD COLUMN brought_by TEXT"),
        ):
            if column not in columns:
                conn.execute(ddl)
                conn.commit()
                log.info("added wines.%s", column)
        # Declared here rather than in SCHEMA: executescript runs before this
        # migration, so on an existing database the column wouldn't exist yet.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wines_tasting ON wines(tasting_id)")
        conn.commit()

    def _migrate_wine_ratings_members(self) -> None:
        """Re-key wine_ratings from a Discord user id onto wine_members.

        The spreadsheet the club's history comes from knows people by first name,
        so a NOT NULL user_id in the primary key made those ratings unstorable.
        Dropping a primary key column means rebuilding the table.

        Any rows already present are carried over: each distinct user id becomes
        a member whose name is a placeholder and whose discord_id is that user,
        so nobody's rating is dropped on the way through.
        """
        conn = self._conn
        assert conn is not None
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(wine_ratings)")}
        if not columns or "user_id" not in columns:
            return

        existing = conn.execute("SELECT * FROM wine_ratings").fetchall()
        for row in existing:
            conn.execute(
                """INSERT INTO wine_members (name, discord_id) VALUES (?, ?)
                   ON CONFLICT(discord_id) DO NOTHING""",
                (f"discord:{row['user_id']}", row["user_id"]),
            )
        conn.executescript(
            """CREATE TABLE wine_ratings_rebuilt (
                   wine_id   INTEGER NOT NULL REFERENCES wines(id) ON DELETE CASCADE,
                   member_id INTEGER NOT NULL REFERENCES wine_members(id) ON DELETE CASCADE,
                   score     INTEGER NOT NULL CHECK (score BETWEEN 1 AND 100),
                   notes     TEXT,
                   rated_at  TEXT    NOT NULL,
                   PRIMARY KEY (wine_id, member_id)
               );"""
        )
        for row in existing:
            member = conn.execute(
                "SELECT id FROM wine_members WHERE discord_id=?", (row["user_id"],)
            ).fetchone()
            conn.execute(
                """INSERT INTO wine_ratings_rebuilt
                       (wine_id, member_id, score, notes, rated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (row["wine_id"], member["id"], row["score"], row["notes"], row["rated_at"]),
            )
        conn.executescript(
            """DROP TABLE wine_ratings;
               ALTER TABLE wine_ratings_rebuilt RENAME TO wine_ratings;"""
        )
        conn.commit()
        log.info("wine_ratings re-keyed onto wine_members (%d row(s) carried over)", len(existing))

    def _migrate_trip_match_city(self) -> None:
        """Add trip_matches.city to a database created before it existed.

        An added nullable column is a plain ALTER; only dropping or changing a
        constraint needs the rebuild `_migrate_trip_year_key` does.
        """
        conn = self._conn
        assert conn is not None
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trip_matches)")}
        if not columns or "city" in columns:
            return
        conn.execute("ALTER TABLE trip_matches ADD COLUMN city TEXT")
        conn.commit()
        log.info("added trip_matches.city")

    def _migrate_trip_year_key(self) -> None:
        """Re-key trips on year alone: we go one place a year.

        The old table had UNIQUE (year, country), which allowed two trips in one
        year. SQLite cannot drop a constraint, so the table is rebuilt. Ids are
        preserved, which is what keeps trip_matches.trip_id valid across the
        swap; foreign keys are disabled for the duration because otherwise
        DROP TABLE trips would cascade every match into oblivion.
        """
        conn = self._conn
        assert conn is not None
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trips'"
        ).fetchone()
        if row is None or "UNIQUE (year, country)" not in (row["sql"] or ""):
            return

        merged = conn.execute(
            "SELECT year, COUNT(*) AS n FROM trips GROUP BY year HAVING n > 1"
        ).fetchall()

        if conn.in_transaction:
            conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            # Refuse rather than risk cascading the matches away.
            log.error("could not disable foreign keys; leaving trips as it is")
            return
        try:
            conn.executescript(
                """CREATE TABLE trips_rebuilt (
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       year      INTEGER NOT NULL UNIQUE,
                       country   TEXT    NOT NULL,
                       city      TEXT,
                       date_from TEXT,
                       date_to   TEXT,
                       notes     TEXT,
                       added_by  INTEGER NOT NULL,
                       added_at  TEXT    NOT NULL
                   );

                   INSERT INTO trips_rebuilt
                   SELECT id, year, country, city, date_from, date_to, notes, added_by, added_at
                   FROM trips WHERE id IN (SELECT MIN(id) FROM trips GROUP BY year);

                   -- Matches of a discarded duplicate move to the kept trip for
                   -- that year rather than being orphaned.
                   UPDATE trip_matches SET trip_id = (
                       SELECT MIN(keeper.id) FROM trips keeper
                       WHERE keeper.year = (
                           SELECT old.year FROM trips old WHERE old.id = trip_matches.trip_id
                       )
                   )
                   WHERE trip_id NOT IN (SELECT id FROM trips_rebuilt);

                   DROP TABLE trips;
                   ALTER TABLE trips_rebuilt RENAME TO trips;
                   CREATE INDEX IF NOT EXISTS idx_trips_year ON trips(year);"""
            )
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                conn.rollback()
                log.error("trips rebuild left %d dangling reference(s), rolled back", len(broken))
                return
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

        for duplicate in merged:
            log.warning(
                "%s had %d trips recorded; merged into one and kept its matches",
                duplicate["year"],
                duplicate["n"],
            )
        log.info("trips re-keyed on year")

    def _migrate_guild_config(self) -> None:
        """Move the old column-per-kind guild_config into guild_channels.

        Idempotent: once the table is gone there is nothing to do. Kept rather
        than simply dropping the table so a database configured before the
        change keeps its channels.
        """
        conn = self._conn
        assert conn is not None
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guild_config'"
        ).fetchone()
        if not exists:
            return
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_config)")}
        for kind in CHANNEL_KINDS:
            column = f"{kind}_channel_id"
            if column not in columns:
                continue
            conn.execute(
                f"""INSERT INTO guild_channels (guild_id, kind, channel_id)
                    SELECT guild_id, ?, {column} FROM guild_config
                    WHERE {column} IS NOT NULL
                    ON CONFLICT(guild_id, kind) DO NOTHING""",
                (kind,),
            )
        conn.execute("DROP TABLE guild_config")
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- thin query helpers ------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.conn.executemany(sql, rows)
        self.conn.commit()

    # -- guild channels ----------------------------------------------------

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in CHANNEL_KINDS:
            raise ValueError(f"unknown channel kind {kind!r}")

    def set_channel(self, guild_id: int, kind: str, channel_id: int) -> None:
        self._check_kind(kind)
        self.execute(
            """INSERT INTO guild_channels (guild_id, kind, channel_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id, kind) DO UPDATE SET channel_id=excluded.channel_id""",
            (guild_id, kind, channel_id),
        )

    def get_channel(self, guild_id: int, kind: str) -> int | None:
        self._check_kind(kind)
        row = self.query_one(
            "SELECT channel_id FROM guild_channels WHERE guild_id=? AND kind=?",
            (guild_id, kind),
        )
        return row["channel_id"] if row else None

    def configured_guilds(self, kind: str) -> list[tuple[int, int]]:
        """[(guild_id, channel_id)] for guilds with this channel kind set."""
        self._check_kind(kind)
        rows = self.query(
            "SELECT guild_id, channel_id FROM guild_channels WHERE kind=? ORDER BY guild_id",
            (kind,),
        )
        return [(r["guild_id"], r["channel_id"]) for r in rows]

    # -- messages kept up to date ------------------------------------------

    def get_bot_message(self, guild_id: int, kind: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM bot_messages WHERE guild_id=? AND kind=?", (guild_id, kind)
        )

    def set_bot_message(
        self, guild_id: int, kind: str, *, channel_id: int, message_id: int, content_hash: str
    ) -> None:
        self.execute(
            """INSERT INTO bot_messages (guild_id, kind, channel_id, message_id,
                                         content_hash, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, kind) DO UPDATE SET
                   channel_id=excluded.channel_id,
                   message_id=excluded.message_id,
                   content_hash=excluded.content_hash,
                   updated_at=excluded.updated_at""",
            (guild_id, kind, channel_id, message_id, content_hash, utcnow_iso()),
        )

    def clear_bot_message(self, guild_id: int, kind: str) -> None:
        self.execute(
            "DELETE FROM bot_messages WHERE guild_id=? AND kind=?", (guild_id, kind)
        )

    # -- members -----------------------------------------------------------

    def set_favourite_team(self, user_id: int, team_id: int | None, team_name: str | None) -> None:
        self.execute(
            """INSERT INTO members (user_id, favourite_team_id, favourite_team_name, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   favourite_team_id=excluded.favourite_team_id,
                   favourite_team_name=excluded.favourite_team_name,
                   updated_at=excluded.updated_at""",
            (user_id, team_id, team_name, utcnow_iso()),
        )

    def favourite_teams(self) -> dict[int, list[int]]:
        """{team_id: [user_id, ...]} across every member who registered a club."""
        rows = self.query(
            "SELECT user_id, favourite_team_id FROM members WHERE favourite_team_id IS NOT NULL"
        )
        out: dict[int, list[int]] = {}
        for row in rows:
            out.setdefault(row["favourite_team_id"], []).append(row["user_id"])
        return out

    # -- matches mirror ----------------------------------------------------

    def upsert_matches(self, matches: Iterable[dict[str, Any]]) -> int:
        """Insert or refresh mirrored matches. Returns the number written."""
        rows = [
            (
                m["match_id"],
                m["competition"],
                m.get("matchday"),
                m.get("home_id"),
                m["home"],
                m.get("away_id"),
                m["away"],
                m["kickoff_utc"],
                m["status"],
                m.get("home_goals"),
                m.get("away_goals"),
                utcnow_iso(),
            )
            for m in matches
        ]
        if not rows:
            return 0
        self.executemany(
            """INSERT INTO matches (match_id, competition, matchday, home_id, home, away_id,
                                    away, kickoff_utc, status, home_goals, away_goals, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(match_id) DO UPDATE SET
                   competition=excluded.competition,
                   matchday=excluded.matchday,
                   home_id=excluded.home_id,
                   home=excluded.home,
                   away_id=excluded.away_id,
                   away=excluded.away,
                   kickoff_utc=excluded.kickoff_utc,
                   status=excluded.status,
                   home_goals=excluded.home_goals,
                   away_goals=excluded.away_goals,
                   updated_at=excluded.updated_at""",
            rows,
        )
        return len(rows)

    # -- events: the evenings we have not held yet -------------------------

    def create_event(
        self,
        *,
        theme: str,
        starts_at: str,
        location: str | None = None,
        host: str | None = None,
        notes: str | None = None,
        created_by: str | None = None,
    ) -> int:
        """Put a planned evening in the diary, returning its id."""
        return self.execute(
            """INSERT INTO events (theme, location, host, starts_at, notes,
                                   created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (theme, location, host, starts_at, notes, created_by, utcnow_iso()),
        )

    def update_event(self, event_id: int, **fields: Any) -> None:
        """Amend an evening. Only the columns passed are touched.

        `None` means "leave it alone" rather than "clear it", the same reading
        `upsert_trip` gives it, so correcting the time cannot wipe the notes.
        Clearing a field is done by passing an empty string.
        """
        allowed = ("theme", "location", "host", "starts_at", "notes")
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not changes:
            return
        assignments = ", ".join(f"{column}=?" for column in changes)
        self.execute(
            f"UPDATE events SET {assignments} WHERE id=?",
            (*(v or None for v in changes.values()), event_id),
        )

    def delete_event(self, event_id: int) -> None:
        """Drop an evening. Its wine list goes with it, by ON DELETE CASCADE."""
        self.execute("DELETE FROM events WHERE id=?", (event_id,))

    def add_event_wine(self, event_id: int, *, name: str, **fields: Any) -> int:
        """Line a bottle up for an evening, at the end of the list."""
        columns = ("producer", "vintage", "country", "region", "grape",
                   "price_nok", "brought_by")
        values = [fields.get(column) for column in columns]
        seat = self.query_one(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM event_wines WHERE event_id=?",
            (event_id,),
        )
        return self.execute(
            f"""INSERT INTO event_wines (event_id, name, {", ".join(columns)},
                                         position, added_at)
                VALUES (?, ?, {", ".join("?" * len(columns))}, ?, ?)""",
            (event_id, name, *values, seat["next"], utcnow_iso()),
        )

    def delete_event_wine(self, event_id: int, wine_id: int) -> None:
        """The event id is in the WHERE too, so a stray id cannot reach another
        evening's list."""
        self.execute(
            "DELETE FROM event_wines WHERE id=? AND event_id=?", (wine_id, event_id)
        )

    def promote_event(self, event_id: int) -> int | None:
        """Move a held evening into the archive, returning the tasting's id.

        The wines become ordinary cellar rows against the new tasting, which is
        what lets them be scored later with the same `wine_ratings` table the
        club's whole history already lives in. `added_by` is the import
        sentinel: a visitor to the web app has no Discord id.

        Returns None if the evening has already been promoted, so pressing the
        button twice cannot leave two copies in the archive.
        """
        event = self.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        if event is None or event["tasting_id"] is not None:
            return None

        when = parse_utc(event["starts_at"])
        theme = (event["theme"] or "").strip()
        key = f"{when.year}-{when.month:02d}|{theme.lower()}"
        self.execute(
            """INSERT INTO tastings (key, year, month, theme, location, host, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   location=COALESCE(excluded.location, tastings.location),
                   host=COALESCE(excluded.host, tastings.host)""",
            (key, when.year, when.month, theme or None, event["location"],
             event["host"], utcnow_iso()),
        )
        tasting_id = self.query_one("SELECT id FROM tastings WHERE key=?", (key,))["id"]

        for wine in self.query(
            "SELECT * FROM event_wines WHERE event_id=? ORDER BY position, id", (event_id,)
        ):
            self.execute(
                """INSERT INTO wines (name, producer, vintage, country, region, grape,
                                      price_nok, tasting_id, brought_by, added_by, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (wine["name"], wine["producer"], wine["vintage"], wine["country"],
                 wine["region"], wine["grape"], wine["price_nok"], tasting_id,
                 wine["brought_by"], utcnow_iso()),
            )
        self.execute("UPDATE events SET tasting_id=? WHERE id=?", (tasting_id, event_id))
        return tasting_id

    # -- trips archive -----------------------------------------------------

    def upsert_trip(
        self,
        *,
        year: int,
        country: str,
        city: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        notes: str | None = None,
        added_by: int,
    ) -> int:
        """Insert or amend the trip for a year, returning its id.

        The year is the key — one place a year — so re-adding 2014 with a
        different country corrects that trip rather than creating a second one.
        Only the fields you supply are written; the rest are left alone, so
        correcting a typo in the city cannot silently wipe the notes.
        """
        self.execute(
            """INSERT INTO trips (year, country, city, date_from, date_to, notes,
                                  added_by, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(year) DO UPDATE SET
                   country=COALESCE(excluded.country, trips.country),
                   city=COALESCE(excluded.city, trips.city),
                   date_from=COALESCE(excluded.date_from, trips.date_from),
                   date_to=COALESCE(excluded.date_to, trips.date_to),
                   notes=COALESCE(excluded.notes, trips.notes)""",
            (year, country, city, date_from, date_to, notes, added_by, utcnow_iso()),
        )
        row = self.trip_by_year(year)
        assert row is not None
        return row["id"]

    def trip_by_year(self, year: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM trips WHERE year=?", (year,))

    def trip_match_rows(self, *, trip_id: int | None = None) -> list[sqlite3.Row]:
        """Matches with their trip's year, country and city joined on.

        Every stats function takes rows in this shape, so there is one query to
        keep correct rather than one per statistic. `city` is the match's own
        when it has one and the trip's otherwise, so no consumer has to repeat
        that fallback; `trip_city` stays available for telling the two apart.
        """
        clause = " WHERE m.trip_id=?" if trip_id is not None else ""
        params = (trip_id,) if trip_id is not None else ()
        return self.query(
            f"""SELECT m.id, m.trip_id, m.match_date, m.competition, m.home, m.away,
                       m.home_goals, m.away_goals, m.stadium, m.attendance, m.notes,
                       m.city AS match_city,
                       t.year AS year, t.country AS country, t.city AS trip_city,
                       COALESCE(m.city, t.city) AS city
                FROM trip_matches m JOIN trips t ON t.id = m.trip_id{clause}
                ORDER BY t.year, m.match_date, m.id""",
            params,
        )

    # -- one-shot announcement guard ---------------------------------------

    def claim_announcement(self, key: str) -> bool:
        """True the first time a key is claimed, False every time after."""
        try:
            self.conn.execute(
                "INSERT INTO announcements (key, sent_at) VALUES (?, ?)", (key, utcnow_iso())
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def claim_reminder(self, match_id: int, kind: str) -> bool:
        """True the first time a (match, kind) reminder is claimed."""
        try:
            self.conn.execute(
                "INSERT INTO reminders_sent (match_id, kind, sent_at) VALUES (?, ?, ?)",
                (match_id, kind, utcnow_iso()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def sql_str_tuple(values: Sequence[str]) -> str:
    """Render a fixed set of literals as a SQL tuple, e.g. ('TIMED', 'SCHEDULED').

    Only ever called with module constants; single quotes are escaped anyway so a
    future edit can't produce broken SQL.
    """
    if not values:
        return "('')"
    inner = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"({inner})"
