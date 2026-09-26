"""The club's history out to YAML and back, with nothing lost on the way.

The claim these files make is that the database can be rebuilt from them. That
is not a thing to assert in a docstring: it is exported, imported and compared
here, against a small database built in a temp directory so the club's own
scores stay out of the repository.
"""

from __future__ import annotations

import pytest

from bot.db import Database
from scripts.export_yaml import EXPORTERS, FILES, HEADERS, _dump
from scripts.import_yaml import restore

NOW = "2026-01-05T19:00:00+00:00"
LATER = "2026-02-05T19:00:00+00:00"


@pytest.fixture()
def cellar(db) -> Database:
    """Everything the format has to carry, in the smallest database that can.

    Deliberately awkward: a guest, a member with a Discord id, an accented name,
    a bottle with every column filled and one with almost none, a rating with a
    note, a wine written at a different moment from the rest of its evening, two
    people who brought the same wine, and an evening nobody scored.
    """
    for name, discord_id, guest in (
        # An invented Discord id: a real one is a real account, and this
        # repository is public.
        ("Andy", None, 0), ("Håvard", 111122223333444455, 0), ("Marius", None, 1),
    ):
        db.execute(
            "INSERT INTO wine_members (name, discord_id, guest) VALUES (?,?,?)",
            (name, discord_id, guest),
        )
    for key, year, month, theme, location, host in (
        ("2019-03|barolo", 2019, 3, "Barolo", "Tore", "Robert"),
        ("2020-00|udatert", 2020, None, "Udatert", None, None),
    ):
        db.execute(
            """INSERT INTO tastings (key, year, month, theme, location, host, added_at)
               VALUES (?,?,?,?,?,?,?)""",
            (key, year, month, theme, location, host, NOW),
        )
    # Pour order is the order they go in, and deliberately not alphabetical.
    wines = [
        # name, producer, vintage, country, region, grape, price, bought, brought, added_at
        ("Zeta Nebbiolo", "Massolino", 2015, "Italy", "Barolo", "Nebbiolo",
         399, "Vinmonopolet", "Tore", NOW),
        ("Alfa Barolo", None, None, None, None, None, None, None, "Håvard", NOW),
        ("Alfa Barolo", None, None, None, None, None, None, None, "Andy", LATER),
    ]
    for row in wines:
        db.execute(
            """INSERT INTO wines (name, producer, vintage, country, region, grape,
                                  price_nok, bought_at, tasting_id, brought_by,
                                  added_by, added_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,0,?)""",
            row,
        )
    db.execute(
        """INSERT INTO wines (name, tasting_id, added_by, added_at)
           VALUES ('Nobody scored me', 2, 7, ?)""",
        (NOW,),
    )
    for wine_id, member_id, score, notes, rated in (
        (1, 1, 88, None, NOW), (1, 2, 91, "smoky, long", NOW),
        (2, 1, 70, None, NOW), (3, 1, 72, None, LATER),
    ):
        db.execute(
            """INSERT INTO wine_ratings (wine_id, member_id, score, notes, rated_at)
               VALUES (?,?,?,?,?)""",
            (wine_id, member_id, score, notes, rated),
        )

    db.execute(
        """INSERT INTO trips (year, country, city, date_from, date_to, notes,
                              added_by, added_at)
           VALUES (2024, 'Spain', 'Bilbao', '2024-04-12', '2024-04-14', 'rain', 42, ?)""",
        (NOW,),
    )
    db.execute("INSERT INTO trips (year, country, added_by, added_at) VALUES (2025,'Italy',42,?)",
               (NOW,))
    db.execute(
        """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                     home_goals, away_goals, city, stadium, attendance, notes)
           VALUES (1,'2024-04-13','La Liga','Athletic Club','Villarreal',2,1,
                   'Bilbao','San Mamés',48000,'end to end')"""
    )
    for minute, scorer, side in ((23, "Williams", "H"), (45, "Sancet", "H"),
                                 (None, "own goal", None)):
        db.execute(
            "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) VALUES (1,?,?,?)",
            (minute, scorer, side),
        )

    db.create_event(kind="tasting", theme="Still to come", starts_at="2027-05-01T17:00:00+00:00",
                    ends_at=None, location="Morten's", host="Morten", country="Norway",
                    notes=None, created_by="Robert")
    db.add_event_wine(1, name="Lined up", vintage=2021, brought_by="Andy")
    db.create_event(kind="trip", theme="Bilbao again", starts_at="2027-06-01T08:00:00+00:00",
                    ends_at="2027-06-03T20:00:00+00:00", location=None, host=None,
                    country="Spain", notes=None, created_by="Tore")
    db.execute("UPDATE events SET tasting_id=1 WHERE id=1")
    db.execute("UPDATE events SET trip_id=1 WHERE id=2")
    return db


def write(db: Database, folder) -> dict[str, str]:
    """Export as the script does, and hand back what each file says."""
    written = {}
    for name in FILES:
        text = _dump(EXPORTERS[name](db), HEADERS[name])
        (folder / f"{name}.yml").write_text(text, encoding="utf-8")
        written[name] = text
    return written


@pytest.fixture()
def rebuilt(cellar, tmp_path) -> Database:
    """The same history, exported and read back into an empty database."""
    folder = tmp_path / "yaml"
    folder.mkdir()
    write(cellar, folder)
    fresh = Database(tmp_path / "rebuilt.sqlite3")
    fresh.connect()
    restore(fresh, folder)
    yield fresh
    fresh.close()


# -- what must survive ------------------------------------------------------

COMPARE = {
    "members": "SELECT name, discord_id, guest FROM wine_members ORDER BY name",
    "tastings": """SELECT key, year, month, theme, location, host, added_at
                   FROM tastings ORDER BY key""",
    "wines": """SELECT t.key, w.name, w.producer, w.vintage, w.country, w.region, w.grape,
                       w.price_nok, w.bought_at, w.brought_by, w.added_by, w.added_at,
                       ROW_NUMBER() OVER (PARTITION BY w.tasting_id ORDER BY w.id) AS seat
                FROM wines w JOIN tastings t ON t.id = w.tasting_id
                ORDER BY t.key, seat""",
    "ratings": """SELECT t.key, w.name, IFNULL(w.brought_by,''), m.name,
                         r.score, r.notes, r.rated_at
                  FROM wine_ratings r JOIN wines w ON w.id = r.wine_id
                  JOIN tastings t ON t.id = w.tasting_id
                  JOIN wine_members m ON m.id = r.member_id
                  ORDER BY t.key, w.name, m.name""",
    "trips": """SELECT year, country, city, date_from, date_to, notes, added_by, added_at
                FROM trips ORDER BY year""",
    "trip_matches": """SELECT t.year, m.match_date, m.competition, m.home, m.away,
                              m.home_goals, m.away_goals, m.city, m.stadium,
                              m.attendance, m.notes
                       FROM trip_matches m JOIN trips t ON t.id = m.trip_id
                       ORDER BY t.year, m.match_date, m.home""",
    "trip_goals": """SELECT t.year, m.home, g.minute, g.scorer, g.side
                     FROM trip_goals g JOIN trip_matches m ON m.id = g.trip_match_id
                     JOIN trips t ON t.id = m.trip_id
                     ORDER BY t.year, m.home, IFNULL(g.minute, 999), g.scorer""",
    "events": """SELECT e.kind, e.theme, e.location, e.host, e.country, e.starts_at,
                        e.ends_at, e.notes, e.created_by, e.created_at, t.key, tr.year
                 FROM events e LEFT JOIN tastings t ON t.id = e.tasting_id
                 LEFT JOIN trips tr ON tr.id = e.trip_id
                 ORDER BY e.starts_at, e.theme""",
    "event_wines": """SELECT e.theme, w.name, w.vintage, w.brought_by, w.position, w.added_at
                      FROM event_wines w JOIN events e ON e.id = w.event_id
                      ORDER BY e.starts_at, w.position""",
}


@pytest.mark.parametrize("table", list(COMPARE))
def test_the_round_trip_loses_nothing(cellar, rebuilt, table):
    """Compared by natural key, so a renumbered database still has to match."""
    sql = COMPARE[table]
    before = [tuple(r) for r in cellar.query(sql)]
    after = [tuple(r) for r in rebuilt.query(sql)]
    assert before, f"the fixture should have some {table} to compare"
    assert after == before


def test_no_row_id_is_written_down(cellar, tmp_path):
    """An id in the file would make every rebuild rewrite the whole thing."""
    written = write(cellar, tmp_path)
    for name, text in written.items():
        for leak in ("tasting_id:", "wine_id:", "member_id:", "trip_id:",
                     "event_id:", "trip_match_id:", "\n  id:", "\n- id:"):
            assert leak not in text, f"{name}.yml writes {leak.strip()}"


def test_re_exporting_changes_nothing(cellar, rebuilt, tmp_path):
    """A file in git must not churn: the same history is the same bytes, even
    after a rebuild has handed every row a different id."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert write(cellar, tmp_path / "a") == write(rebuilt, tmp_path / "b")


def test_a_null_is_absent_rather_than_written_out(cellar, tmp_path):
    written = write(cellar, tmp_path)
    assert "null" not in written["wines"], "a missing field should simply not be there"
    assert "month:" in written["wines"], "but a month that exists is still written"


def test_the_order_they_were_poured_survives(cellar, rebuilt):
    """Not alphabetical, and the page now reads the order off the row ids."""
    poured = [w["name"] for w in rebuilt.query(
        "SELECT name FROM wines WHERE tasting_id=1 ORDER BY id")]
    assert poured == ["Zeta Nebbiolo", "Alfa Barolo", "Alfa Barolo"]


def test_two_people_brought_the_same_wine(cellar, rebuilt):
    """The pair the survey importer already guards: same name, different bottle."""
    both = rebuilt.query(
        "SELECT brought_by FROM wines WHERE name='Alfa Barolo' ORDER BY brought_by")
    assert [r["brought_by"] for r in both] == ["Andy", "Håvard"]


def test_a_note_on_a_score_survives(cellar, rebuilt, tmp_path):
    written = write(cellar, tmp_path)
    assert "smoky, long" in written["wines"]
    kept = rebuilt.query_one("SELECT notes FROM wine_ratings WHERE notes IS NOT NULL")
    assert kept["notes"] == "smoky, long"


def test_a_guest_is_still_a_guest(cellar, rebuilt):
    assert rebuilt.query_one("SELECT guest FROM wine_members WHERE name='Marius'")["guest"] == 1
    assert rebuilt.query_one("SELECT guest FROM wine_members WHERE name='Andy'")["guest"] == 0


def test_an_event_still_points_at_its_tasting(cellar, rebuilt):
    """By key, not by id — every id in the rebuilt database is a new one."""
    event = rebuilt.query_one("SELECT tasting_id FROM events WHERE theme='Still to come'")
    tasting = rebuilt.query_one("SELECT id FROM tastings WHERE key='2019-03|barolo'")
    assert event["tasting_id"] == tasting["id"]
    trip_event = rebuilt.query_one("SELECT trip_id FROM events WHERE theme='Bilbao again'")
    assert trip_event["trip_id"] == rebuilt.query_one(
        "SELECT id FROM trips WHERE year=2024")["id"]


def test_a_score_by_a_stranger_is_refused(cellar, tmp_path):
    """Rather than inventing a tenth member out of a typo."""
    import yaml

    folder = tmp_path / "yaml"
    folder.mkdir()
    write(cellar, folder)
    path = folder / "wines.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["tastings"][0]["wines"][0]["scores"]["Nobody"] = 90
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    fresh = Database(tmp_path / "broken.sqlite3")
    fresh.connect()
    with pytest.raises(SystemExit, match="Nobody"):
        restore(fresh, folder)
    fresh.close()
