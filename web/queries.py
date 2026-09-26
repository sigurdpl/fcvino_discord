"""Every read the web app makes, in one place.

Returns `sqlite3.Row`s and plain tuples — no Jinja, no FastAPI — so the SQL can
be exercised against a real database in tests without a browser.

These tables belong to the bot: `bot/db.py` owns the schema and the migrations.
Nothing here writes, and nothing here assumes a column the bot does not already
create.
"""

from __future__ import annotations

import sqlite3

from bot import trip_stats, wine_stats
from bot.db import Database, utcnow_iso

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

# Every score, by name, for the wines they were given to. Nine opinions rather
# than the one number they average to, which is what the "scored by" filter
# needs — and 8469 rows, beside the 1172 the index already holds.
WINE_SCORES = """
    SELECT r.wine_id, m.name, r.score
    FROM wine_ratings r JOIN wine_members m ON m.id = r.member_id
"""

# Cheap enough to run on every search: if neither the number of wines nor the
# number of ratings has moved, the index cannot be stale. This is what lets the
# *bot* write to the same database while the web app is running.
#
# `MAX(rated_at)` is in here because a *changed* score leaves both counts where
# they were — and a card resubmitted during an evening does exactly that. The
# average has always had that hole; a named person's number on the page makes
# it much easier to notice.
FINGERPRINT = """
    SELECT (SELECT COUNT(*) FROM wines)            AS wines,
           (SELECT COUNT(*) FROM wine_ratings)     AS ratings,
           (SELECT MAX(added_at) FROM wines)       AS latest,
           (SELECT MAX(rated_at) FROM wine_ratings) AS scored
"""


def fingerprint(db: Database) -> tuple:
    row = db.query_one(FINGERPRINT)
    assert row is not None
    return (row["wines"], row["ratings"], row["latest"], row["scored"])


def build_index(db: Database) -> Index:
    scores: dict[int, dict[str, int]] = {}
    for rating in db.query(WINE_SCORES):
        scores.setdefault(rating["wine_id"], {})[rating["name"]] = rating["score"]
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
            scores=scores.get(row["id"], {}),
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
    """An evening's bottles, best first — and where there is no best, in order.

    The tie-break is the row's own id rather than its name, which is the order
    the bottles were written down and so the order they were poured. It only
    shows on a wine nobody scored; the debut of July 2012 is a whole evening of
    them, and alphabetical would open it on the Crozes-Hermitage.
    """
    return db.query(
        """SELECT w.*, AVG(r.score) AS average, COUNT(r.score) AS ratings
           FROM wines w LEFT JOIN wine_ratings r ON r.wine_id = w.id
           WHERE w.tasting_id = ?
           GROUP BY w.id ORDER BY average DESC NULLS LAST, w.id""",
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
                  (SELECT COUNT(*) FROM wine_members
                    WHERE NOT guest)                   AS members,
                  (SELECT MIN(year) FROM tastings)     AS first_year,
                  (SELECT MAX(year) FROM tastings)     AS last_year"""
    )


def members(db: Database) -> list[sqlite3.Row]:
    """The club. Guests rated a few bottles once and are not on this list."""
    return db.query("SELECT id, name FROM wine_members WHERE NOT guest ORDER BY name")


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


# -- events -----------------------------------------------------------------


def events(db: Database) -> list[sqlite3.Row]:
    """Every planned evening, soonest first, with its bottle count."""
    return db.query(
        """SELECT e.*, COUNT(w.id) AS wines
           FROM events e LEFT JOIN event_wines w ON w.event_id = e.id
           GROUP BY e.id ORDER BY e.starts_at"""
    )


def upcoming_events(db: Database, limit: int = 4) -> list[sqlite3.Row]:
    """The next few evenings, soonest first.

    `utcnow_iso()` is already the shape `starts_at` is stored in, so the two
    compare as text without parsing either.
    """
    return db.query(
        """SELECT e.*, COUNT(w.id) AS wines
           FROM events e LEFT JOIN event_wines w ON w.event_id = e.id
           WHERE e.starts_at >= ?
           GROUP BY e.id ORDER BY e.starts_at LIMIT ?""",
        (utcnow_iso(), limit),
    )


def event(db: Database, event_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def event_wines(db: Database, event_id: int) -> list[sqlite3.Row]:
    return db.query(
        "SELECT * FROM event_wines WHERE event_id = ? ORDER BY position, id",
        (event_id,),
    )


def my_votes(db: Database, event_id: int, member_id: int) -> dict[int, int]:
    """What this member has already said, so the page comes back as they left it."""
    return {
        row["event_wine_id"]: row["score"]
        for row in db.query(
            """SELECT v.event_wine_id, v.score FROM event_votes v
               JOIN event_wines w ON w.id = v.event_wine_id
               WHERE w.event_id = ? AND v.member_id = ?""",
            (event_id, member_id),
        )
    }


def who_has_voted(db: Database, event_id: int) -> list[str]:
    """The names, and only the names.

    The host needs to know when everyone is done. Nobody may see a number
    before the reveal, least of all on a blind evening, so no score comes back
    from here — not even an average.
    """
    return [
        row["name"]
        for row in db.query(
            """SELECT DISTINCT m.name FROM event_votes v
               JOIN event_wines w ON w.id = v.event_wine_id
               JOIN wine_members m ON m.id = v.member_id
               WHERE w.event_id = ? ORDER BY m.name""",
            (event_id,),
        )
    ]


# -- trips ------------------------------------------------------------------


def trips(db: Database) -> list[sqlite3.Row]:
    return db.query("SELECT * FROM trips ORDER BY year DESC")


def trip(db: Database, year: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM trips WHERE year = ?", (year,))


def trip_summary(db: Database) -> dict:
    """The headline numbers, all from `bot/trip_stats.py`.

    Lives here rather than in the trips route because the landing page wants the
    same counts, and two places counting trips independently is how two pages
    end up disagreeing.
    """
    rows = trips(db)
    matches = db.trip_match_rows()
    wins, draws, losses = trip_stats.result_split(matches)
    years = trip_stats.years(rows)
    return {
        "trips": rows,
        "matches": matches,
        "countries": trip_stats.country_counts(rows),
        "clubs": trip_stats.club_counts(matches),
        "cities": trip_stats.distinct_cities(matches),
        "goals": trip_stats.total_goals(matches),
        "goals_per_game": trip_stats.goals_per_game(matches),
        "grounds": trip_stats.distinct_stadiums(matches),
        "played": trip_stats.played(matches),
        "result_split": (wins, draws, losses),
        "streak": trip_stats.longest_year_streak(years),
        "biggest_win": trip_stats.biggest_win(matches),
        "highest_scoring": trip_stats.highest_scoring(matches),
        "years": years,
    }


# -- the landing page -------------------------------------------------------


def top_wines(db: Database, limit: int = 5) -> list[sqlite3.Row]:
    """Bottles by group average, best first.

    The same question `/wine top` answers in Discord, asked the same way, so the
    two halves cannot put a different bottle at the top.
    """
    return db.query(
        """SELECT w.*, AVG(r.score) AS average, COUNT(r.score) AS ratings
           FROM wines w JOIN wine_ratings r ON r.wine_id = w.id
           GROUP BY w.id HAVING ratings >= ?
           ORDER BY average DESC, ratings DESC, w.name LIMIT ?""",
        (wine_stats.MIN_RATINGS, limit),
    )


def recent_activity(db: Database, limit: int = 4) -> list[dict]:
    """The club's last few evenings and away trips, newest first, interleaved.

    The two have different notions of when: a tasting knows a year and usually a
    month, a trip knows a departure date. Both collapse to (year, month) for
    ordering, which is as fine as the tastings ever get.
    """
    feed: list[dict] = []
    for row in tastings(db)[: limit * 2]:
        feed.append({
            "kind": "Tasting",
            "name": row["theme"] or "A tasting",
            "year": row["year"],
            "month": row["month"],
            "detail": f"{row['wines']} wines",
            "href": f"/wine/tastings/{row['id']}",
        })
    for row in trips(db)[: limit * 2]:
        month = None
        if row["date_from"] and len(row["date_from"]) >= 7:
            month = int(row["date_from"][5:7])
        where = ", ".join(part for part in (row["country"], row["city"]) if part)
        feed.append({
            "kind": "Away trip",
            "name": where or str(row["year"]),
            "year": row["year"],
            "month": month,
            "detail": row["notes"] or "",
            "href": f"/trips/{row['year']}",
        })
    feed.sort(key=lambda item: (item["year"] or 0, item["month"] or 0), reverse=True)
    return feed[:limit]


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
