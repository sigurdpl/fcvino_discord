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

SCHEMA_VERSION = 4

SCHEMA = """
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
    added_by    INTEGER NOT NULL,
    added_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wines_name ON wines(name);

CREATE TABLE IF NOT EXISTS wine_ratings (
    wine_id   INTEGER NOT NULL REFERENCES wines(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL,
    score     INTEGER NOT NULL CHECK (score BETWEEN 1 AND 100),
    notes     TEXT,
    rated_at  TEXT    NOT NULL,
    PRIMARY KEY (wine_id, user_id)
);

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

CHANNEL_KINDS = ("football", "wine", "predictions", "standings")


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
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

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
