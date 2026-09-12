"""The web app end to end: signing in, the gate, and every page.

Runs against a real (temporary) database built by `bot/db.py`, so the SQL in
`web/queries.py` is exercised rather than mocked — a column renamed in the bot's
schema has to show up here as a failure.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from bot import config
from bot.db import Database, utcnow_iso
from web.app import create_app

PASSWORD = "grenache noir"


@pytest.fixture()
def cellar(tmp_path) -> Database:
    """A small but real cellar: two evenings, three wines, three members."""
    db = Database(tmp_path / "web.sqlite3")
    db.connect()
    now = utcnow_iso()

    for name in ("Andy", "Morten", "Tore"):
        db.execute("INSERT INTO wine_members (name) VALUES (?)", (name,))
    for key, year, month, theme, location in (
        ("2019-03|barolo", 2019, 3, "Barolo", "Tore"),
        ("2020-05|musar", 2020, 5, "Ch. Musar", "Robert"),
    ):
        db.execute(
            "INSERT INTO tastings (key, year, month, theme, location, added_at)"
            " VALUES (?,?,?,?,?,?)",
            (key, year, month, theme, location, now),
        )
    for name, country, region, grape, vintage, tasting_id, brought in (
        ("Massolino Barolo 2015", "Italy", "Barolo", "Nebbiolo", 2015, 1, "Tore"),
        ("Boroli Barolo 2005", "Italy", "Barolo", "Nebbiolo", 2005, 1, None),
        ("Ch. Musar 2005", "Lebanon", "Bekaa Valley", None, 2005, 2, "Morten"),
    ):
        db.execute(
            """INSERT INTO wines (name, country, region, grape, vintage, tasting_id,
                                  brought_by, added_by, added_at)
               VALUES (?,?,?,?,?,?,?,0,?)""",
            (name, country, region, grape, vintage, tasting_id, brought, now),
        )
    scores = {1: (88, 90, 86), 2: (80, 82, 78), 3: (94, 92, 91)}
    for wine_id, trio in scores.items():
        for member_id, score in enumerate(trio, start=1):
            db.execute(
                "INSERT INTO wine_ratings (wine_id, member_id, score, notes, rated_at)"
                " VALUES (?,?,?,?,?)",
                (wine_id, member_id, score, f"note {member_id}", now),
            )

    db.upsert_trip(year=2024, country="Spain", city="Bilbao", added_by=0)
    trip = db.trip_by_year(2024)
    db.execute(
        """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                     home_goals, away_goals, stadium)
           VALUES (?,?,?,?,?,?,?,?)""",
        (trip["id"], "2024-04-13", "La Liga", "Athletic Club", "Villarreal", 2, 1, "San Mamés"),
    )
    db.execute(
        """INSERT INTO matches (match_id, competition, matchday, home, away,
                                kickoff_utc, status, home_goals, away_goals, updated_at)
           VALUES (1,'PL',10,'Arsenal','Liverpool','2026-10-01T19:00:00Z','FINISHED',2,1,?)""",
        (now,),
    )
    yield db
    db.close()


@pytest.fixture()
def client(cellar, tmp_path):
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3",
        web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one",
        football_token=None,          # no network from the test suite
    )
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture()
def signed_in(client):
    client.post("/login", data={"password": PASSWORD})
    return client


PAGES = ["/wine", "/wine/boards", "/wine/tastings", "/wine/tastings/1", "/wine/1",
         "/trips", "/trips/2024", "/football"]


# -- the gate ---------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_every_page_needs_a_login(client, path):
    """Nine people's tasting notes are not public, even on localhost."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_login_page_itself_is_reachable(client):
    assert client.get("/login").status_code == 200


def test_a_wrong_password_is_refused(client):
    response = client.post("/login", data={"password": "not it"})
    assert response.status_code == 401
    assert client.get("/wine", follow_redirects=False).status_code == 303


def test_the_right_password_lets_you_in(client):
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/wine").status_code == 200


def test_the_password_is_compared_whole(client):
    # A prefix of the real password must not be accepted.
    assert client.post("/login", data={"password": PASSWORD[:5]}).status_code == 401


def test_you_are_sent_back_where_you_were_going(client):
    client.get("/trips", follow_redirects=False)
    landing = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert landing.headers["location"] == "/trips"


def test_an_offsite_redirect_is_not_honoured(client):
    """The "come back where you were" hop must never leave the site."""
    client.post("/login", data={"password": PASSWORD})
    response = client.post(
        "/whoami", data={"member_id": 1},
        headers={"referer": "https://example.com/evil"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/wine"


def test_signing_out_clears_the_session(signed_in):
    signed_in.get("/logout")
    assert signed_in.get("/wine", follow_redirects=False).status_code == 303


# -- who you are ------------------------------------------------------------


def test_claiming_a_name_sticks(signed_in):
    signed_in.post("/whoami", data={"member_id": 2})
    assert "Morten" in signed_in.get("/wine").text


def test_an_unknown_member_id_is_ignored(signed_in):
    signed_in.post("/whoami", data={"member_id": 999})
    page = signed_in.get("/wine").text
    assert "haven't said who you are" in page


def test_you_cannot_claim_a_name_without_the_password(client):
    client.post("/whoami", data={"member_id": 1}, follow_redirects=False)
    assert client.get("/wine", follow_redirects=False).status_code == 303


# -- the pages --------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(signed_in, path):
    assert signed_in.get(path).status_code == 200


def test_the_cellar_lists_its_wines(signed_in):
    page = signed_in.get("/wine").text
    assert "Massolino Barolo 2015" in page
    assert "3 wines" in page


def test_search_narrows_the_list(signed_in):
    page = signed_in.get("/wine", params={"q": "musar"}).text
    assert "Ch. Musar 2005" in page
    assert "Massolino" not in page


def test_search_folds_the_query(signed_in):
    assert "Ch. Musar 2005" in signed_in.get("/wine", params={"q": "bekaa"}).text


def test_a_filter_narrows_the_list(signed_in):
    # Asserted against the results fragment: the full page also carries every
    # value in its filter dropdowns, so "Barolo" is on it either way.
    rows = signed_in.get("/wine/results", params={"country": "Lebanon"}).text
    assert "Ch. Musar" in rows
    assert "Barolo" not in rows


def test_the_htmx_fragment_is_the_table_alone(signed_in):
    fragment = signed_in.get("/wine/results", params={"q": "barolo"}).text
    assert "Massolino Barolo 2015" in fragment
    assert "<html" not in fragment.lower(), "a fragment, not a whole page"


def test_a_wine_page_shows_every_score_and_note(signed_in):
    page = signed_in.get("/wine/3").text
    for who, score in (("Andy", "94"), ("Morten", "92"), ("Tore", "91")):
        assert who in page and score in page
    assert "note 1" in page


def test_a_wine_page_shows_the_group_average(signed_in):
    assert "92.3" in signed_in.get("/wine/3").text


def test_a_board_needs_enough_bottles_to_say_anything(signed_in):
    """Three wines is not a verdict on Italy, and the board must not pretend."""
    page = signed_in.get("/wine/boards").text
    assert "Nothing qualifies yet" in page


def test_a_board_appears_once_there_are_enough(signed_in, cellar):
    now = utcnow_iso()
    for i in range(5):
        cellar.execute(
            """INSERT INTO wines (name, country, grape, tasting_id, added_by, added_at)
               VALUES (?,?,?,?,0,?)""",
            (f"Chablis {i}", "France", "Chardonnay", 1 + (i % 2), now),
        )
        wine_id = cellar.query_one("SELECT MAX(id) AS id FROM wines")["id"]
        cellar.execute(
            "INSERT INTO wine_ratings (wine_id, member_id, score, rated_at) VALUES (?,1,?,?)",
            (wine_id, 90, now),
        )
    page = signed_in.get("/wine/boards").text
    assert "France" in page and "Chardonnay" in page


def test_a_missing_wine_is_a_404(signed_in):
    assert signed_in.get("/wine/9999").status_code == 404


def test_a_missing_tasting_is_a_404(signed_in):
    assert signed_in.get("/wine/tastings/9999").status_code == 404


def test_a_missing_trip_is_a_404(signed_in):
    assert signed_in.get("/trips/1999").status_code == 404


def test_the_trips_page_shows_the_match(signed_in):
    page = signed_in.get("/trips").text
    assert "Spain" in page and "Athletic Club" in page


def test_a_trip_page_shows_its_ground(signed_in):
    assert "San Mamés" in signed_in.get("/trips/2024").text


def test_football_survives_having_no_api_token(signed_in):
    """No FOOTBALL_DATA_TOKEN must degrade the table, not the page."""
    page = signed_in.get("/football")
    assert page.status_code == 200
    assert "unavailable" in page.text
    assert "Arsenal" in page.text, "the mirrored result still shows"


def test_the_root_redirects_to_the_cellar(signed_in):
    assert signed_in.get("/", follow_redirects=False).headers["location"] == "/wine"


# -- the index tracks the database -----------------------------------------


def test_a_wine_added_behind_the_app_shows_up(signed_in, cellar):
    """The bot writes to the same file while the web app is running."""
    assert "Cloudy Bay" not in signed_in.get("/wine", params={"q": "cloudy"}).text
    cellar.execute(
        "INSERT INTO wines (name, country, added_by, added_at) VALUES (?,?,0,?)",
        ("Cloudy Bay Chardonnay 2018", "New Zealand", utcnow_iso()),
    )
    assert "Cloudy Bay" in signed_in.get("/wine", params={"q": "cloudy"}).text


def test_a_rating_added_behind_the_app_moves_the_average(signed_in, cellar):
    assert "80.0" in signed_in.get("/wine/2").text, "80, 82 and 78"
    cellar.execute("INSERT INTO wine_members (name) VALUES ('Lennart')")
    cellar.execute(
        """INSERT INTO wine_ratings (wine_id, member_id, score, rated_at)
           VALUES (2, (SELECT id FROM wine_members WHERE name='Lennart'), 100, ?)""",
        (utcnow_iso(),),
    )
    page = signed_in.get("/wine/2").text
    assert "85.0" in page, "80, 82, 78 and 100"
    assert "Lennart" in page
