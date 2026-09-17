"""The web app end to end: signing in, the gate, and every page.

Runs against a real (temporary) database built by `bot/db.py`, so the SQL in
`web/queries.py` is exercised rather than mocked — a column renamed in the bot's
schema has to show up here as a failure.
"""

from __future__ import annotations

import dataclasses
import re

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
        # Pinned, not inherited: config.load reads the developer's own .env, and
        # once that has FCVINO_ACCESS=1 for the real deployment this fixture
        # would quietly become the Access one — right down to a Secure-only
        # cookie that never survives http in a test.
        access_trusted=False,
        access_members={},
    )
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture()
def signed_in(client):
    client.post("/login", data={"password": PASSWORD})
    return client


PAGES = ["/", "/wine", "/wine/boards", "/wine/tastings", "/wine/tastings/1", "/wine/1",
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


# -- Cloudflare Access as the front door ------------------------------------

ACCESS_HEADER = {"Cf-Access-Authenticated-User-Email": "Morten@Example.COM"}


@pytest.fixture()
def behind_access(cellar, tmp_path):
    """The app as it runs in production: Cloudflare vouches, we believe it."""
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3",
        web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one",
        football_token=None,
        access_trusted=True,
        access_members={"morten@example.com": "Morten", "nobody@example.com": "Nigel"},
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_a_vouched_email_needs_no_password(behind_access):
    assert behind_access.get("/wine", headers=ACCESS_HEADER).status_code == 200


def test_a_vouched_email_is_already_a_name(behind_access):
    """No password page and no dropdown: the address says who you are."""
    page = behind_access.get("/wine", headers=ACCESS_HEADER).text
    assert "Morten" in page
    assert "haven't said who you are" not in page


def test_the_header_alone_is_not_enough(client):
    """Off the tunnel the header is just a header anyone could have typed."""
    response = client.get("/wine", headers=ACCESS_HEADER, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_behind_access_a_bare_request_still_meets_the_gate(behind_access):
    assert behind_access.get("/wine", follow_redirects=False).status_code == 303


def test_an_address_mapped_to_nobody_we_know_is_signed_in_but_unnamed(behind_access):
    """A typo in the .env mapping leaves you anonymous, not somebody else."""
    page = behind_access.get(
        "/wine", headers={"Cf-Access-Authenticated-User-Email": "nobody@example.com"}
    ).text
    assert "haven't said who you are" in page


def test_an_unmapped_address_still_gets_in(behind_access):
    """Access already decided they belong; the name is the only open question."""
    response = behind_access.get(
        "/wine", headers={"Cf-Access-Authenticated-User-Email": "guest@example.com"}
    )
    assert response.status_code == 200
    assert "haven't said who you are" in response.text


def test_signing_out_goes_through_cloudflare(behind_access):
    """Clearing our session alone would be a revolving door — Access still knows."""
    response = behind_access.get(
        "/logout", headers=ACCESS_HEADER, follow_redirects=False
    )
    assert response.headers["location"] == "/cdn-cgi/access/logout"


def test_signing_out_locally_still_goes_to_the_login_page(signed_in):
    response = signed_in.get("/logout", follow_redirects=False)
    assert response.headers["location"] == "/login"


# -- reading the member off the address -------------------------------------
#
# The club's addresses are already listed in the Cloudflare policy; making
# someone list them again in .env just to be recognised is duplication. Most of
# them start with the member's own first name, so that is where the name comes
# from, and only the addresses that don't need writing down.


@pytest.fixture()
def unmapped(cellar, tmp_path):
    """Behind Access with FCVINO_ACCESS_MEMBERS empty — the intended setup."""
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3",
        web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one",
        football_token=None,
        access_trusted=True,
        access_members={},
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def claimed_name(client, address: str) -> str | None:
    """Who the app thinks you are, or None if it is still asking.

    Read out of the header rather than off the whole page: every member's name
    appears in the "who are you?" dropdown, so `"Morten" in page` would pass
    even when nothing had been matched at all. That prompt's absence is the
    reliable signal, and the header carries the name itself.
    """
    page = client.get(
        "/wine", headers={"Cf-Access-Authenticated-User-Email": address}
    ).text
    if "haven't said who you are" in page:
        return None
    found = re.search(r'class="avatar sm"[^>]*title="([^"]+)"', page)
    return found.group(1) if found else None


def test_the_address_names_you_with_no_mapping_at_all(unmapped):
    assert claimed_name(unmapped, "morten@example.com") == "Morten"


def test_an_accent_in_the_name_is_folded_past(unmapped, cellar):
    """`havard@` has to find Håvard — nobody puts å in an address."""
    cellar.execute("INSERT INTO wine_members (name) VALUES ('Håvard')")
    assert claimed_name(unmapped, "havard@example.com") == "Håvard"


def test_a_firstname_lastname_address_still_lands(unmapped):
    assert claimed_name(unmapped, "morten.hestmann@work.example.com") == "Morten"


def test_a_prefix_is_not_a_match(unmapped):
    """Tore must not be claimed by tor@ — folded, but always whole."""
    assert claimed_name(unmapped, "tor@example.com") is None


def test_an_address_that_says_nothing_leaves_you_to_pick(unmapped):
    assert claimed_name(unmapped, "post@fcvino.no") is None


def test_the_mapping_overrules_the_address(cellar, tmp_path):
    """A wrong guess has to be fixable, and .env is the only place to fix it."""
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3",
        web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one",
        football_token=None,
        access_trusted=True,
        access_members={"morten@example.com": "Tore"},
    )
    with TestClient(create_app(cfg)) as client:
        assert claimed_name(client, "morten@example.com") == "Tore"


# -- the search form as a browser actually submits it -----------------------
#
# Every filter field is sent on every search, touched or not, so an untouched
# `<input type="number">` arrives as `year_from=`. Parsed strictly that is not a
# number, and the whole request was refused before it reached the index — which
# looked like the search box simply doing nothing, because htmx won't swap in a
# 4xx and the form's own submit goes through htmx too.

BLANK_FILTERS = {
    "country": "", "region": "", "grape": "", "member": "",
    "year_from": "", "year_to": "", "vintage_from": "", "vintage_to": "",
    "min_score": "",
}


@pytest.mark.parametrize("path", ["/wine", "/wine/results"])
def test_searching_with_every_filter_left_blank(signed_in, path):
    response = signed_in.get(path, params={"q": "barolo", **BLANK_FILTERS})
    assert response.status_code == 200
    assert "Massolino Barolo 2015" in response.text


def test_a_filter_still_works_when_its_neighbours_are_blank(signed_in):
    """Blank must mean "no filter", not "no results"."""
    rows = signed_in.get(
        "/wine/results", params={**BLANK_FILTERS, "q": "", "min_score": "90"}
    ).text
    assert "Ch. Musar 2005" in rows      # averages 92.3
    assert "Boroli Barolo 2005" not in rows  # averages 80.0


def test_a_number_that_is_not_a_number_is_still_refused(signed_in):
    """Blank is meaningful; nonsense is not, and must not read as no filter."""
    response = signed_in.get("/wine/results", params={"min_score": "vintage"})
    assert response.status_code == 422


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


def test_the_root_is_the_landing_page(signed_in):
    """It used to bounce to /wine; now the front door is a page of its own."""
    response = signed_in.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Same passion" in response.text


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


# -- the club-page chrome ---------------------------------------------------


@pytest.mark.parametrize("path", ["/wine", "/trips", "/football", "/wine/boards"])
def test_the_group_header_carries_the_same_numbers_everywhere(signed_in, path):
    """Like a group's member count: the same on every page, not just the one
    route that happened to query for it."""
    page = signed_in.get(path).text
    assert "3 bottles" in page and "9 ratings" in page


def test_the_login_page_has_no_group_header(signed_in, client):
    client.get("/logout")
    page = client.get("/login").text
    assert "bottles ·" not in page, "nothing about the club before you are let in"


def test_a_wine_with_notes_reads_as_a_discussion(signed_in):
    page = signed_in.get("/wine/3").text
    assert "What everyone said" in page
    assert 'class="said"' in page, "the note is drawn as a bubble"
    assert 'class="notes "' in page or 'class="notes"' in page


def test_a_wine_without_notes_collapses(signed_in, cellar):
    """7456 imported ratings carry no note at all — the sheet never had them.

    A thread with nothing said in it is a tall list of numbers, so the page must
    tighten up rather than leave nine empty bubbles.
    """
    cellar.execute("UPDATE wine_ratings SET notes = NULL WHERE wine_id = 3")
    page = signed_in.get("/wine/3").text
    assert "What everyone scored it" in page
    assert 'class="said"' not in page
    assert "notes tight" in page


def test_every_rater_gets_an_avatar(signed_in):
    page = signed_in.get("/wine/3").text
    for who, letter in (("Andy", "A"), ("Morten", "M"), ("Tore", "T")):
        assert who in page
        assert f'>{letter}</span>' in page


def test_avatar_colours_are_inline_and_stable(signed_in):
    from web.avatars import colour
    page = signed_in.get("/wine/3").text
    assert f"background: {colour('Morten')}" in page


# -- the landing page -------------------------------------------------------
#
# It restates numbers that other pages own — bottles, tastings, trips — so the
# tests that matter are the ones proving it reads them from the same queries
# rather than keeping a second copy that can drift.


def test_the_landing_page_counts_agree_with_the_cellar(signed_in, cellar):
    from web import queries
    page = signed_in.get("/").text
    totals = queries.cellar_totals(cellar)
    for number in (totals["wines"], totals["ratings"], totals["tastings"], totals["members"]):
        assert str(number) in page


def test_the_landing_page_names_the_club_bottles(signed_in):
    """The mockup listed Romanée-Conti; this has to be what we actually drank."""
    page = signed_in.get("/").text
    assert "Ch. Musar 2005" in page          # averages 92.3, the cellar's best
    assert "Romanée-Conti" not in page


def test_the_landing_page_lists_recent_evenings_and_trips(signed_in):
    page = signed_in.get("/").text
    assert "Ch. Musar" in page               # the May 2020 tasting
    assert "Spain, Bilbao" in page           # the 2024 trip


def test_top_wines_needs_more_than_one_opinion(cellar):
    """A bottle one person loved must not outrank one the whole club scored."""
    from bot import wine_stats
    from web import queries
    cellar.execute(
        """INSERT INTO wines (name, country, tasting_id, added_by, added_at)
           VALUES ('A Lone Opinion', 'France', 1, 0, '2020-01-01T00:00:00+00:00')"""
    )
    wine_id = cellar.query_one("SELECT id FROM wines WHERE name = 'A Lone Opinion'")["id"]
    cellar.execute(
        "INSERT INTO wine_ratings (wine_id, member_id, score, rated_at)"
        " VALUES (?, 1, 100, '2020-01-01T00:00:00+00:00')",
        (wine_id,),
    )
    names = [row["name"] for row in queries.top_wines(cellar, 10)]
    assert "A Lone Opinion" not in names
    assert wine_stats.MIN_RATINGS == 2


def test_top_wines_are_ordered_by_the_group_average(cellar):
    from web import queries
    rows = queries.top_wines(cellar, 10)
    assert [row["name"] for row in rows][0] == "Ch. Musar 2005"
    assert [row["average"] for row in rows] == sorted(
        (row["average"] for row in rows), reverse=True
    )


def test_recent_activity_interleaves_both_kinds_newest_first(cellar):
    from web import queries
    feed = queries.recent_activity(cellar, 10)
    assert {item["kind"] for item in feed} == {"Tasting", "Away trip"}
    keys = [(item["year"], item["month"] or 0) for item in feed]
    assert keys == sorted(keys, reverse=True)


def test_the_bot_and_the_web_agree_on_what_qualifies(cellar):
    """One number, one place — the cog now takes it from bot.wine_stats."""
    from bot import wine_stats
    from bot.cogs.wine import MIN_RATINGS_FOR_BOARD
    assert MIN_RATINGS_FOR_BOARD is wine_stats.MIN_RATINGS


def test_the_masthead_says_its_name_without_the_logo_file(signed_in):
    """The pictures are not in the repository, so the bar cannot depend on one.

    A CSS background with transparent text leaves a checkout without the logo
    showing nothing at all where the club's name should be; an <img> falls back
    to its alt text on its own.
    """
    page = signed_in.get("/wine").text
    assert re.search(r'class="brand"[^>]*>\s*<img [^>]*alt="FC Vino"', page)
