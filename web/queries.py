"""Every read the web app makes, in one place.

Returns `sqlite3.Row`s and plain tuples — no Jinja, no FastAPI — so the SQL can
be exercised against a real database in tests without a browser.

These tables belong to the bot: `bot/db.py` owns the schema and the migrations.
Nothing here writes, and nothing here assumes a column the bot does not already
create.
"""

from __future__ import annotations

import sqlite3

from bot.db import Database

from .search import Index, Row

# -- the wine index ---------------------------------------------------------

WINE_ROWS = """
    SELECT w.id, w.name, w.country, w.region, w.grape, w.vintage, w.brought_by,
           t.theme AS theme, t.year AS year,
           AVG(r.score) AS average, COUNT(r.score) AS ratings
    FROM wines w
    LEFT JOIN tastings t ON t.id = w.tasting_id
    LEFT JOIN wine_ratings r ON r.wine_id = w.id
    GROUP BY w.id
"""

# Cheap enough to run on every search: if neither the number of wines nor the
# number of ratings has moved, the index cannot be stale. This is what lets the
# *bot* write to the same database while the web app is running.
FINGERPRINT = """
    SELECT (SELECT COUNT(*) FROM wines)        AS wines,
           (SELECT COUNT(*) FROM wine_ratings) AS ratings,
           (SELECT MAX(added_at) FROM wines)   AS latest
"""


def fingerprint(db: Database) -> tuple:
    row = db.query_one(FINGERPRINT)
    assert row is not None
    return (row["wines"], row["ratings"], row["latest"])


def build_index(db: Database) -> Index:
    return Index(
        Row(
            id=row["id"],
            name=row["name"],
            country=row["country"],
            region=row["region"],
            grape=row["grape"],
            vintage=row["vintage"],
            theme=row["theme"],
            year=row["year"],
            brought_by=row["brought_by"],
            average=row["average"],
            ratings=row["ratings"],
        )
        for row in db.query(WINE_ROWS)
    )


class IndexCache:
    """One search index per process, rebuilt when the database moves under it."""

    def __init__(self) -> None:
        self._index: Index | None = None
        self._stamp: tuple | None = None

    def get(self, db: Database) -> Index:
        stamp = fingerprint(db)
        if self._index is None or stamp != self._stamp:
            self._index = build_index(db)
            self._stamp = stamp
        return self._index

    def invalidate(self) -> None:
        self._index = None
        self._stamp = None


# -- one wine ---------------------------------------------------------------


def wine(db: Database, wine_id: int) -> sqlite3.Row | None:
    return db.query_one(
        """SELECT w.*, t.theme, t.year, t.month, t.location, t.host, t.id AS tasting
           FROM wines w LEFT JOIN tastings t ON t.id = w.tasting_id
           WHERE w.id = ?""",
        (wine_id,),
    )


def wine_ratings(db: Database, wine_id: int) -> list[sqlite3.Row]:
    """Everyone's score and notes for one bottle, best first."""
    return db.query(
        """SELECT m.name, r.score, r.notes, r.rated_at
           FROM wine_ratings r JOIN wine_members m ON m.id = r.member_id
           WHERE r.wine_id = ? ORDER BY r.score DESC, m.name""",
        (wine_id,),
    )


def wine_summary(db: Database, wine_id: int) -> sqlite3.Row | None:
    return db.query_one(
        """SELECT AVG(score) AS average, COUNT(*) AS ratings,
                  MIN(score) AS lowest, MAX(score) AS highest
           FROM wine_ratings WHERE wine_id = ?""",
        (wine_id,),
    )


def same_wine_elsewhere(db: Database, wine_id: int) -> list[sqlite3.Row]:
    """The same bottle poured at another evening.

    Three separate Ch. Musar 2005 rows scored 92.3, 85.0 and 65.8 — worth
    putting in front of the reader rather than leaving them to notice.
    """
    return db.query(
        """SELECT w.id, t.year, t.month, t.theme, AVG(r.score) AS average,
                  COUNT(r.score) AS ratings
           FROM wines w
           LEFT JOIN tastings t ON t.id = w.tasting_id
           LEFT JOIN wine_ratings r ON r.wine_id = w.id
           WHERE lower(w.name) = (SELECT lower(name) FROM wines WHERE id = ?)
             AND w.id != ?
           GROUP BY w.id ORDER BY t.year, t.month""",
        (wine_id, wine_id),
    )


# -- tastings ---------------------------------------------------------------


def tastings(db: Database) -> list[sqlite3.Row]:
    return db.query(
        """SELECT t.*, COUNT(DISTINCT w.id) AS wines,
                  AVG(r.score) AS average, COUNT(r.score) AS ratings
           FROM tastings t
           LEFT JOIN wines w ON w.tasting_id = t.id
           LEFT JOIN wine_ratings r ON r.wine_id = w.id
           GROUP BY t.id ORDER BY t.year DESC, t.month DESC"""
    )


def tasting(db: Database, tasting_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM tastings WHERE id = ?", (tasting_id,))


def tasting_wines(db: Database, tasting_id: int) -> list[sqlite3.Row]:
    return db.query(
        """SELECT w.*, AVG(r.score) AS average, COUNT(r.score) AS ratings
           FROM wines w LEFT JOIN wine_ratings r ON r.wine_id = w.id
           WHERE w.tasting_id = ?
           GROUP BY w.id ORDER BY average DESC NULLS LAST, w.name""",
        (tasting_id,),
    )


# -- the boards -------------------------------------------------------------

BOARD_ROWS = """
    SELECT w.{field} AS key, w.id AS wine_id, w.tasting_id AS tasting_id,
           r.score AS score
    FROM wines w JOIN wine_ratings r ON r.wine_id = w.id
    WHERE w.{field} IS NOT NULL
"""

BOARD_FIELDS = ("country", "region", "grape")


def board_rows(db: Database, field: str) -> list[sqlite3.Row]:
    """Rating rows bucketed by one column, for `bot.wine_stats.tally`.

    The field is interpolated because SQLite cannot parameterise a column name,
    so it is checked against BOARD_FIELDS first and never arrives as free text.
    """
    if field not in BOARD_FIELDS:
        raise ValueError(f"not a board: {field!r}")
    return db.query(BOARD_ROWS.format(field=field))


def cellar_totals(db: Database) -> sqlite3.Row | None:
    return db.query_one(
        """SELECT (SELECT COUNT(*) FROM wines)         AS wines,
                  (SELECT COUNT(*) FROM wine_ratings)  AS ratings,
                  (SELECT COUNT(*) FROM tastings)      AS tastings,
                  (SELECT COUNT(*) FROM wine_members)  AS members,
                  (SELECT MIN(year) FROM tastings)     AS first_year,
                  (SELECT MAX(year) FROM tastings)     AS last_year"""
    )


def members(db: Database) -> list[sqlite3.Row]:
    return db.query("SELECT id, name FROM wine_members ORDER BY name")


def member_ratings(db: Database, member_id: int, limit: int = 100) -> list[sqlite3.Row]:
    return db.query(
        """SELECT w.id, w.name, w.country, w.region, w.grape, w.vintage,
                  r.score, r.notes, t.year, t.theme
           FROM wine_ratings r
           JOIN wines w ON w.id = r.wine_id
           LEFT JOIN tastings t ON t.id = w.tasting_id
           WHERE r.member_id = ? ORDER BY r.score DESC LIMIT ?""",
        (member_id, limit),
    )


# -- trips ------------------------------------------------------------------


def trips(db: Database) -> list[sqlite3.Row]:
    return db.query("SELECT * FROM trips ORDER BY year DESC")


def trip(db: Database, year: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM trips WHERE year = ?", (year,))


# -- football ---------------------------------------------------------------


def fixtures(db: Database, limit: int = 20) -> list[sqlite3.Row]:
    """Upcoming matches from the local mirror the bot keeps — no API call."""
    return db.query(
        """SELECT * FROM matches
           WHERE status IN ('SCHEDULED', 'TIMED')
           ORDER BY kickoff_utc LIMIT ?""",
        (limit,),
    )


def results(db: Database, limit: int = 20) -> list[sqlite3.Row]:
    return db.query(
        """SELECT * FROM matches
           WHERE status = 'FINISHED'
           ORDER BY kickoff_utc DESC LIMIT ?""",
        (limit,),
    )


def competitions(db: Database) -> list[str]:
    return [row["competition"] for row in db.query(
        "SELECT DISTINCT competition FROM matches ORDER BY competition"
    )]
