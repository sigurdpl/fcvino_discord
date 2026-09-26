"""The web app end to end: signing in, the gate, and every page.

Runs against a real (temporary) database built by `bot/db.py`, so the SQL in
`web/queries.py` is exercised rather than mocked — a column renamed in the bot's
schema has to show up here as a failure.
"""

from __future__ import annotations

import dataclasses
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
        anthropic_key=None,           # nor for reading a label
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


PAGES = ["/", "/events", "/wine", "/wine/boards", "/wine/tastings", "/wine/tastings/1", "/wine/1",
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
    "country": "", "region": "", "grape": "", "brought_by": "", "rated_by": "",
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


def test_the_stylesheet_url_is_stamped_with_the_files_version(signed_in):
    """Markup and stylesheet ship together but cache apart.

    A browser holding yesterday's CSS against today's HTML is not a cosmetic
    mismatch: it has already drawn the wordmark twice and collapsed four images
    to nothing. A stamped URL means the old file cannot be served for the new
    page, so the two can never disagree.
    """
    page = signed_in.get("/wine").text
    assert re.search(r'href="/static/style_dark2\.css\?v=[0-9a-f]+"', page)
    assert re.search(r'src="/static/htmx\.min\.js\?v=[0-9a-f]+"', page)


def test_the_stamp_follows_the_file(signed_in, tmp_path):
    """Touching the stylesheet has to change the URL, or it buys nothing."""
    from web import deps
    before = deps.static_url("style_dark2.css")
    sheet = deps.WEB_ROOT / "static" / "style_dark2.css"
    original = sheet.stat()
    try:
        os.utime(sheet, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
        assert deps.static_url("style_dark2.css") != before
    finally:
        os.utime(sheet, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert deps.static_url("style_dark2.css") == before


def test_a_missing_static_file_still_yields_a_usable_url(signed_in):
    """The club's pictures are legitimately absent elsewhere; don't raise."""
    from web import deps
    assert deps.static_url("not-here.png") == "/static/not-here.png"


# -- events: the evenings we have not held yet ------------------------------
#
# The app's first write path, so these check that a write happened *and* that a
# rejected one left nothing behind.

SOON = "2099-10-12T19:00"
GONE = "2020-03-05T19:00"


def tonight(hour: str = "19:00") -> str:
    """Today's date in the club's timezone, as the form writes it.

    An evening can only be started on its own day, so a test about scoring one
    has to date it today — which is what those tests always meant, even while
    they said 2099. `SOON` stays for the diary tests, where being in the future
    is the whole point.
    """
    return f"{datetime.now(ZoneInfo('Europe/Oslo')).date().isoformat()}T{hour}"


def make_event(client, when=SOON, theme="Moden Piemonte", **extra):
    data = {"theme": theme, "starts_at": when, "location": "Thomas's", "host": "Morten"}
    return client.post("/events", data={**data, **extra}, follow_redirects=False)


def only_event(db):
    rows = db.query("SELECT * FROM events")
    assert len(rows) == 1, f"expected one event, found {len(rows)}"
    return rows[0]


def test_registering_an_evening_puts_it_in_the_diary(signed_in, cellar):
    response = make_event(signed_in)
    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    row = only_event(cellar)
    assert row["theme"] == "Moden Piemonte"
    assert row["location"] == "Thomas's"
    assert row["host"] == "Morten"
    assert "Moden Piemonte" in signed_in.get("/events").text


def test_the_evening_is_stored_as_the_instant_not_the_wall_clock(signed_in, cellar):
    """19:00 in Oslo in October is 17:00 UTC — stored UTC, shown local."""
    make_event(signed_in)
    assert only_event(cellar)["starts_at"].startswith("2099-10-12T17:00")
    assert "19:00" in signed_in.get("/events").text


def test_an_event_needs_a_name(signed_in, cellar):
    """"Theme" was right when every event was a tasting; a trip has a
    destination and a Julebord has a title."""
    response = make_event(signed_in, theme="   ")
    assert response.status_code == 400
    assert "needs a name" in response.text
    assert cellar.query("SELECT * FROM events") == []


def test_an_unreadable_date_writes_nothing(signed_in, cellar):
    response = make_event(signed_in, when="whenever")
    assert response.status_code == 400
    assert cellar.query("SELECT * FROM events") == []


def test_editing_an_evening_changes_it(signed_in, cellar):
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}", data={
        "theme": "Barolo instead", "starts_at": "2099-11-01T18:30",
        "location": "Erk's", "host": "Tore",
    })
    row = only_event(cellar)
    assert row["theme"] == "Barolo instead"
    assert row["location"] == "Erk's"
    assert row["starts_at"].startswith("2099-11-01T17:30")


def test_deleting_an_evening_takes_its_wines_with_it(signed_in, cellar):
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Produttori 2019"})
    assert cellar.query("SELECT * FROM event_wines") != []

    signed_in.post(f"/events/{event_id}/delete")
    assert cellar.query("SELECT * FROM events") == []
    assert cellar.query("SELECT * FROM event_wines") == [], "ON DELETE CASCADE"


def test_a_bottle_can_be_lined_up_with_the_cellar_s_details(signed_in, cellar):
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={
        "name": "Produttori del Barbaresco", "producer": "Produttori",
        "vintage": "2019", "country": "Italy", "region": "Barbaresco",
        "grape": "Nebbiolo", "price_nok": "420", "brought_by": "Thomas",
    })
    (wine,) = cellar.query("SELECT * FROM event_wines")
    assert wine["name"] == "Produttori del Barbaresco"
    assert wine["vintage"] == 2019
    assert wine["price_nok"] == 420
    assert wine["grape"] == "Nebbiolo"
    assert "Produttori del Barbaresco" in signed_in.get(f"/events/{event_id}").text


def test_a_bottle_needs_a_name(signed_in, cellar):
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    response = signed_in.post(f"/events/{event_id}/wines", data={"name": " "})
    assert response.status_code == 400
    assert cellar.query("SELECT * FROM event_wines") == []


def test_a_bottle_cannot_be_removed_from_another_evening(signed_in, cellar):
    """The event id is in the WHERE, so a stray id reaches nothing."""
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Stays put"})
    (wine,) = cellar.query("SELECT * FROM event_wines")

    signed_in.post(f"/events/{event_id + 99}/wines/{wine['id']}/delete")
    assert cellar.query("SELECT * FROM event_wines") != []


def test_a_planned_evening_is_not_counted_as_one_we_held(signed_in, cellar):
    """The whole reason events are their own table: the cellar's headline
    numbers must keep meaning what we actually drank."""
    from web import queries
    before = dict(queries.cellar_totals(cellar))
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Not drunk yet"})
    assert dict(queries.cellar_totals(cellar)) == before


def test_a_held_evening_moves_into_the_archive(signed_in, cellar):
    make_event(signed_in, when=GONE, theme="Sørlige Rhône")
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={
        "name": "Dom. Santa Duc", "vintage": "2019", "brought_by": "Morten",
    })
    signed_in.post(f"/events/{event_id}/close")

    (tasting,) = cellar.query("SELECT * FROM tastings WHERE theme = 'Sørlige Rhône'")
    assert (tasting["year"], tasting["month"]) == (2020, 3)
    (wine,) = cellar.query("SELECT * FROM wines WHERE tasting_id = ?", (tasting["id"],))
    assert wine["name"] == "Dom. Santa Duc"
    assert wine["brought_by"] == "Morten"
    assert only_event(cellar)["tasting_id"] == tasting["id"]


def test_an_evening_cannot_be_archived_twice(signed_in, cellar):
    make_event(signed_in, when=GONE, theme="Sørlige Rhône")
    event_id = only_event(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Dom. Santa Duc"})
    signed_in.post(f"/events/{event_id}/close")
    signed_in.post(f"/events/{event_id}/close")

    assert len(cellar.query("SELECT * FROM tastings WHERE theme = 'Sørlige Rhône'")) == 1
    assert len(cellar.query("SELECT * FROM wines WHERE name = 'Dom. Santa Duc'")) == 1


def test_an_evening_still_to_come_offers_no_archive_button(signed_in, cellar):
    make_event(signed_in)
    event_id = only_event(cellar)["id"]
    assert "Move to the archive" not in signed_in.get(f"/events/{event_id}").text


def test_writing_an_event_needs_a_login(client, cellar):
    client.post("/events", data={"theme": "Sneaky", "starts_at": SOON},
                follow_redirects=False)
    assert cellar.query("SELECT * FROM events") == []


def test_an_evening_is_filed_under_whoever_registered_it(behind_access, cellar):
    """Behind Access the gate is also what claims the name, so the author must
    be read after it — on a session's first request, not before."""
    behind_access.post("/events", headers=ACCESS_HEADER,
                       data={"theme": "Named", "starts_at": SOON})
    assert only_event(cellar)["created_by"] == "Morten"


# -- guests are not members -------------------------------------------------
#
# Someone who came to an evening or three without ever joining. Their scores
# are part of what those bottles averaged and stay; the club's count and its
# pickers are about membership, and leave them out.


@pytest.fixture()
def with_guest(cellar):
    """Andy, Morten, Tore — plus a guest who rated one bottle."""
    cellar.execute("INSERT INTO wine_members (name, guest) VALUES ('Marius', 1)")
    guest = cellar.query_one("SELECT id FROM wine_members WHERE name='Marius'")["id"]
    cellar.execute(
        "INSERT INTO wine_ratings (wine_id, member_id, score, rated_at)"
        " VALUES (3, ?, 70, '2014-06-01T00:00:00+00:00')",
        (guest,),
    )
    return cellar


def test_a_guest_is_not_counted_as_a_member(with_guest):
    from web import queries
    assert queries.cellar_totals(with_guest)["members"] == 3, "Andy, Morten, Tore"
    assert with_guest.query_one("SELECT COUNT(*) AS n FROM wine_members")["n"] == 4


def test_a_guest_is_not_offered_as_one(with_guest):
    from web import queries
    assert "Marius" not in [m["name"] for m in queries.members(with_guest)]


def test_a_guests_scores_are_kept(with_guest):
    """They are part of what that bottle averaged — removing them would quietly
    change a number the club recorded years ago."""
    from web import queries
    ratings = queries.wine_ratings(with_guest, 3)
    assert "Marius" in [r["name"] for r in ratings]
    assert queries.wine_summary(with_guest, 3)["ratings"] == 4


def test_the_home_page_counts_the_club_not_the_guests(signed_in, cellar):
    cellar.execute("INSERT INTO wine_members (name, guest) VALUES ('Marius', 1)")
    page = signed_in.get("/").text
    import re as _re
    tile = _re.search(r'<div class="n">(\d+)</div><div class="l">Members</div>', page)
    assert tile and tile.group(1) == "3"


def test_a_guest_cannot_be_claimed_as_your_name(signed_in, cellar):
    """The 'who are you?' dropdown is the club, so a guest is not in it."""
    cellar.execute("INSERT INTO wine_members (name, guest) VALUES ('Marius', 1)")
    assert "Marius" not in signed_in.get("/wine").text
# -- the home page's upcoming panel -----------------------------------------


def test_the_home_page_shows_what_is_coming_up(signed_in):
    make_event(signed_in, theme="Moden Piemonte")
    page = signed_in.get("/").text
    assert "Upcoming" in page
    assert "Moden Piemonte" in page


def test_an_evening_already_held_is_not_upcoming(signed_in):
    make_event(signed_in, when=GONE, theme="Long gone")
    assert "Long gone" not in signed_in.get("/").text


def test_only_the_next_four_evenings_are_listed(signed_in):
    for month in range(1, 6):          # five evenings, registered out of order
        make_event(signed_in, when=f"2099-{6 - month:02d}-01T19:00",
                   theme=f"Evening {6 - month}")
    page = signed_in.get("/").text
    shown = [n for n in range(1, 6) if f"Evening {n}" in page]
    assert shown == [1, 2, 3, 4], "the four soonest, and not the fifth"
    assert page.index("Evening 1") < page.index("Evening 4"), "soonest first"


def test_an_empty_diary_says_so_and_points_at_the_page(signed_in):
    page = signed_in.get("/").text
    assert "Nothing in the diary" in page
    assert "Register an evening" in page


def test_the_map_of_where_we_have_been_is_gone(signed_in):
    page = signed_in.get("/").text
    assert "Where we have been" not in page
    assert "fv-map" not in page


# -- four kinds of event ----------------------------------------------------


def kinded(client, kind, **extra):
    data = {"kind": kind, "theme": f"A {kind}", "starts_at": SOON}
    return client.post("/events", data={**data, **extra}, follow_redirects=False)


def latest(db):
    return db.query("SELECT * FROM events ORDER BY id DESC")[0]


@pytest.mark.parametrize("kind", ["tasting", "blind", "trip", "other"])
def test_each_kind_can_be_registered(signed_in, cellar, kind):
    extra = {"country": "Belgium"} if kind == "trip" else {}
    assert kinded(signed_in, kind, **extra).status_code == 303
    assert latest(cellar)["kind"] == kind
    assert f"A {kind}" in signed_in.get("/events").text


def test_an_unknown_kind_is_refused(signed_in, cellar):
    assert kinded(signed_in, "banquet").status_code == 400
    assert cellar.query("SELECT * FROM events") == []


# -- blind: the bottles are not named until it is archived ------------------


def blind_with_bottles(client, db, when=GONE):
    kinded(client, "blind", theme="Blind night", starts_at=when)
    event_id = latest(db)["id"]
    # Names that appear nowhere else on the page — the add-a-bottle form's
    # placeholder is itself a wine name, and would match a careless assertion.
    for name in ("Vega Sicilia Unico", "Pingus Ribera"):
        client.post(f"/events/{event_id}/wines",
                    data={"name": name, "vintage": "2019", "grape": "Nebbiolo",
                          "brought_by": "Thomas"})
    return event_id


def test_a_blind_evening_does_not_name_its_bottles(signed_in, cellar):
    """The whole point. The names are in the database; the page will not say."""
    event_id = blind_with_bottles(signed_in, cellar)
    page = signed_in.get(f"/events/{event_id}").text
    assert "Vega Sicilia Unico" not in page
    assert "Pingus Ribera" not in page
    assert "Nebbiolo" not in page
    assert "Wine 1" in page and "Wine 2" in page
    assert "Thomas" in page, "who brought it still shows, so the list is usable"
    assert cellar.query("SELECT name FROM event_wines")[0]["name"] == "Vega Sicilia Unico"


def test_archiving_a_blind_evening_names_them(signed_in, cellar):
    event_id = blind_with_bottles(signed_in, cellar)
    signed_in.post(f"/events/{event_id}/close")
    page = signed_in.get(f"/events/{event_id}").text
    assert "Vega Sicilia Unico" in page
    assert "Wine 1" not in page
    assert cellar.query("SELECT * FROM wines WHERE name = 'Vega Sicilia Unico'") != []


def test_an_ordinary_tasting_names_its_bottles_all_along(signed_in, cellar):
    """The regression that matters: nothing about a normal evening changed."""
    make_event(signed_in, theme="Not blind")
    event_id = latest(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Ch. Musar 2005"})
    assert "Ch. Musar 2005" in signed_in.get(f"/events/{event_id}").text


# -- trips go to the archive of seventeen -----------------------------------


def test_a_trip_needs_a_country(signed_in, cellar):
    """Without one it could never be filed, and finding that out later is no use."""
    response = kinded(signed_in, "trip")
    assert response.status_code == 400
    assert "needs a country" in response.text
    assert cellar.query("SELECT * FROM events") == []


def test_archiving_a_trip_puts_it_with_the_others(signed_in, cellar):
    kinded(signed_in, "trip", theme="Brugge", starts_at="2027-03-06T08:00",
           ends_at="2027-03-08T20:00", country="Belgium", location="Brugge")
    event_id = latest(cellar)["id"]
    signed_in.post(f"/events/{event_id}/close")

    trip = cellar.query_one("SELECT * FROM trips WHERE year = 2027")
    assert trip["country"] == "Belgium"
    assert trip["city"] == "Brugge"
    assert (trip["date_from"], trip["date_to"]) == ("2027-03-06", "2027-03-08")
    assert latest(cellar)["trip_id"] == trip["id"]
    assert cellar.query("SELECT * FROM tastings WHERE theme = 'Brugge'") == [], \
        "a trip is not an evening in the cellar"


def test_a_trip_is_not_archived_twice(signed_in, cellar):
    kinded(signed_in, "trip", theme="Brugge", starts_at="2027-03-06T08:00",
           country="Belgium")
    event_id = latest(cellar)["id"]
    signed_in.post(f"/events/{event_id}/close")
    signed_in.post(f"/events/{event_id}/close")
    assert len(cellar.query("SELECT * FROM trips WHERE year = 2027")) == 1


def test_a_trip_shows_its_span_not_a_departure(signed_in, cellar):
    kinded(signed_in, "trip", theme="Brugge", starts_at="2027-03-06T08:00",
           ends_at="2027-03-08T20:00", country="Belgium")
    assert "6–8 March 2027" in signed_in.get("/events").text


# -- something else ---------------------------------------------------------


def test_a_free_form_event_has_no_bottles_and_no_archive(signed_in, cellar):
    kinded(signed_in, "other", theme="Julebord", starts_at=GONE,
           notes="Dress code: awful jumpers")
    page = signed_in.get(f"/events/{latest(cellar)['id']}").text
    assert "Dress code: awful jumpers" in page
    assert "Add a bottle" not in page
    assert "Move to the archive" not in page


# -- Norway is where the club is --------------------------------------------


def test_the_register_form_arrives_with_norway_in_it(signed_in):
    assert 'name="country" value="Norway"' in signed_in.get("/events").text


def test_a_tasting_registered_untouched_is_in_norway(signed_in, cellar):
    make_event(signed_in, theme="Moden Piemonte", country="Norway")
    assert latest(cellar)["country"] == "Norway"


def test_the_diary_says_nothing_about_being_in_norway(signed_in, cellar):
    """True of nearly every row, so printing it says nothing and costs a column."""
    # No apostrophe in the location: Jinja escapes it, and the escaping is not
    # what this test is about.
    make_event(signed_in, theme="At home", location="Lennart", country="Norway")
    # The form's box and the script both legitimately say Norway, so look at
    # the table element alone.
    page = signed_in.get("/events").text
    table = page.split("Coming up")[1].split("</table>")[0]
    assert "Lennart" in table
    assert "Norway" not in table


def test_the_diary_does_say_when_it_is_somewhere_else(signed_in, cellar):
    kinded(signed_in, "trip", theme="Brugge", location="Brugge", country="Belgium")
    assert "Brugge, Belgium" in signed_in.get("/events").text


def test_an_evening_held_abroad_keeps_its_country(signed_in, cellar):
    make_event(signed_in, theme="Vinsmaking i Italia", location="Alba", country="Italy")
    assert latest(cellar)["country"] == "Italy"
    assert "Alba, Italy" in signed_in.get("/events").text


@pytest.mark.parametrize("location, country, expected", [
    ("Thomas's", "Norway", "Thomas's"),
    ("Thomas's", "  norway ", "Thomas's"),      # however it was typed
    ("Brugge", "Belgium", "Brugge, Belgium"),
    (None, "Belgium", "Belgium"),
    ("Erk's", None, "Erk's"),
    (None, None, "—"),
])
def test_how_a_place_reads(location, country, expected):
    from web.deps import fmt_where
    assert fmt_where(location, country) == expected


def test_hidden_really_hides(signed_in):
    """The per-kind fields are hidden with the `hidden` attribute, and a class
    rule setting `display` outranks the browser's own `[hidden]` rule — so
    without this the attribute is set and the field stays on screen."""
    css = signed_in.get("/static/style_dark2.css").text
    assert "[hidden] { display: none !important; }" in css


def test_an_end_on_the_same_day_is_an_end_time(signed_in, cellar):
    """A Julebord running 18:00 to 22:59 is one evening, not "28–28 January"."""
    kinded(signed_in, "other", theme="Julebord",
           starts_at="2027-01-28T18:00", ends_at="2027-01-28T22:59")
    page = signed_in.get("/events").text
    assert "18:00–22:59" in page
    assert "28–28" not in page


# -- photographing the label ------------------------------------------------
#
# No test calls the API. The route reaches the reader through the module, so
# these replace it: what matters here is that a read fills the form in and
# writes nothing, and that every way it can fail leaves the bottle addable by
# hand.

JPEG = ("bottle.jpg", b"not really a jpeg, and never looked at", "image/jpeg")


@pytest.fixture()
def with_camera(cellar, tmp_path):
    """The app as it runs with a key configured — the only way to reach it."""
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3",
        web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one",
        football_token=None,
        access_trusted=False,
        access_members={},
        anthropic_key="test-key-never-used",
    )
    with TestClient(create_app(cfg)) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


@pytest.fixture()
def evening(with_camera, cellar):
    """An evening with bottles, ready for one to be photographed onto it."""
    make_event(with_camera, theme="Labels")
    return latest(cellar)["id"]


def reads(monkeypatch, **fields):
    """Stand the reader down and have it return this label.

    Every field is filled in, because the model's are required and nullable —
    it has to say "the label doesn't give one" rather than quietly leave it out.
    """
    from web import label as label_module

    blank = dict.fromkeys(label_module.Label.model_fields)

    def fake(image, media_type, api_key):
        assert image, "the photo should reach the reader"
        return label_module.Label(**{**blank, **fields})

    monkeypatch.setattr(label_module, "read_label", fake)


def refuses(monkeypatch, message="Couldn't read a wine off that one."):
    from web import label as label_module

    def fake(image, media_type, api_key):
        raise label_module.LabelUnreadable(message)

    monkeypatch.setattr(label_module, "read_label", fake)


def never_called(monkeypatch):
    from web import label as label_module

    def fake(image, media_type, api_key):
        raise AssertionError("the reader was called; it should have been refused first")

    monkeypatch.setattr(label_module, "read_label", fake)


def shoot(client, event_id, photo=JPEG):
    return client.post(f"/events/{event_id}/wines/label", files={"photo": photo})


def test_the_camera_is_offered_where_a_key_is_configured(with_camera, evening):
    assert "Photograph the label" in with_camera.get(f"/events/{evening}").text


def test_without_a_key_the_page_is_as_it_was(signed_in, cellar):
    """The feature follows its configuration, the way /football already does."""
    make_event(signed_in, theme="No camera")
    page = signed_in.get(f"/events/{latest(cellar)['id']}").text
    assert "Photograph the label" not in page
    assert "Add" in page, "typing a bottle in never depends on the key"


def test_without_a_key_the_route_is_not_there(signed_in, cellar):
    make_event(signed_in, theme="No camera")
    assert shoot(signed_in, latest(cellar)["id"]).status_code == 404


def test_a_read_label_fills_the_form_in(with_camera, evening, monkeypatch):
    reads(monkeypatch, name="Produttori del Barbaresco 2019", producer="Produttori",
          vintage=2019, country="Italy", region="Barbaresco", grape="Nebbiolo")
    page = shoot(with_camera, evening).text
    for value in ("Produttori del Barbaresco 2019", "Produttori", "2019",
                  "Italy", "Barbaresco", "Nebbiolo"):
        assert f'value="{value}"' in page


def test_reading_a_label_writes_nothing(with_camera, evening, cellar, monkeypatch):
    """A vision model reading a decorative label can be confidently wrong, so
    the bottle only exists once somebody has pressed Add."""
    reads(monkeypatch, name="Ch. Musar 2005")
    assert shoot(with_camera, evening).status_code == 200
    assert cellar.query("SELECT * FROM event_wines") == []


def field(page, name):
    """The value attribute of the add-a-bottle form's `name` box.

    Scoped to that form: the event's own details are edited further up the page
    and have a country box of their own, which already carries "Norway".
    """
    form = page.split('/wines">')[-1]     # the add-a-bottle form's own action
    tag = re.search(rf'<input name="{name}"[^>]*>', form, re.S)
    assert tag, f"no {name} box on the page"
    found = re.search(r'value="([^"]*)"', tag.group(0))
    return found.group(1) if found else ""


def test_what_the_label_did_not_say_stays_empty(with_camera, evening, monkeypatch):
    """A guessed vintage is worse than a blank box: the club records vintages."""
    reads(monkeypatch, name="Domaine sans millésime")
    page = shoot(with_camera, evening).text
    assert 'value="Domaine sans millésime"' in page
    assert field(page, "vintage") == ""
    assert field(page, "country") == ""
    assert field(page, "grape") == ""


def test_the_filled_form_still_adds_the_bottle(with_camera, evening, cellar, monkeypatch):
    """What the read is for — the same form and the same write as by hand."""
    reads(monkeypatch, name="Pingus Ribera", vintage=2019)
    shoot(with_camera, evening)
    with_camera.post(f"/events/{evening}/wines",
                     data={"name": "Pingus Ribera", "vintage": "2019"})
    row = cellar.query("SELECT * FROM event_wines")[0]
    assert (row["name"], row["vintage"]) == ("Pingus Ribera", 2019)


def test_a_label_it_cannot_read_says_so(with_camera, evening, monkeypatch):
    refuses(monkeypatch)
    response = shoot(with_camera, evening)
    assert response.status_code == 400
    assert "Couldn&#39;t read a wine off that one." in response.text
    assert "Photograph the label" in response.text, "and you can try another photo"
    assert 'name="name"' in response.text, "or type it in"


def test_something_that_is_not_a_photo_is_refused_before_any_call(
    with_camera, evening, monkeypatch
):
    never_called(monkeypatch)
    response = shoot(with_camera, evening, ("notes.pdf", b"%PDF-1.4", "application/pdf"))
    assert response.status_code == 400
    assert "isn&#39;t a photo" in response.text


def test_an_oversized_photo_is_refused_before_any_call(with_camera, evening, monkeypatch):
    from web import label as label_module

    never_called(monkeypatch)
    huge = b"\xff" * (label_module.MAX_BYTES + 1)
    response = shoot(with_camera, evening, ("huge.jpg", huge, "image/jpeg"))
    assert response.status_code == 400
    assert "too big" in response.text


def test_no_photo_at_all(with_camera, evening, monkeypatch):
    never_called(monkeypatch)
    response = with_camera.post(f"/events/{evening}/wines/label", data={})
    assert response.status_code == 400
    assert "No photo came through." in response.text


def test_a_photo_for_an_evening_that_is_not_there(with_camera, monkeypatch):
    never_called(monkeypatch)
    assert shoot(with_camera, 9999).status_code == 404


def test_the_camera_needs_a_login(cellar, tmp_path, monkeypatch):
    """Nine people's evenings, and a key that costs money on every call."""
    never_called(monkeypatch)
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "web.sqlite3", web_password=PASSWORD,
        web_secret="test-secret-not-a-real-one", football_token=None,
        access_trusted=False, access_members={}, anthropic_key="test-key-never-used",
    )
    with TestClient(create_app(cfg)) as c:
        response = c.post("/events/1/wines/label", files={"photo": JPEG},
                          follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# -- an evening nobody scored ----------------------------------------------


def test_an_unscored_evening_keeps_its_pouring_order(signed_in, cellar):
    """The debut of July 2012 has no scores at all, so every average is null and
    the tie-break is all there is. Alphabetical would throw away the one thing
    the rows do record — the order the bottles were opened in."""
    now = utcnow_iso()
    cellar.execute(
        "INSERT INTO tastings (key, year, month, theme, added_at) VALUES (?,?,?,?,?)",
        ("2012-07|debut", 2012, 7, "Debut", now),
    )
    tasting = cellar.query_one("SELECT id FROM tastings WHERE key='2012-07|debut'")["id"]
    poured = ["Zinfandel first", "Doppio Passo second", "Alain Graillot last"]
    for name in poured:
        cellar.execute(
            """INSERT INTO wines (name, tasting_id, added_by, added_at)
               VALUES (?,?,0,?)""",
            (name, tasting, now),
        )
    page = signed_in.get(f"/wine/tastings/{tasting}").text
    assert [n for n in poured if n in page] == poured, "all three are on the page"
    assert [page.index(n) for n in poured] == sorted(page.index(n) for n in poured)


# -- the voting page --------------------------------------------------------
#
# The first thing the app does *during* an evening. What these pin is the line
# between the staging tables and the cellar: a card can be changed all night,
# and nothing reaches fifteen years of history until somebody closes the
# evening.


def ready_to_vote(client, db, kind="tasting", bottles=("Alfa", "Beta", "Gamma")):
    """An evening with bottles, started, waiting for cards."""
    kinded(client, kind, theme=f"Scoring {kind}", starts_at=tonight())
    event_id = latest(db)["id"]
    for name in bottles:
        client.post(f"/events/{event_id}/wines", data={"name": name})
    client.post(f"/events/{event_id}/start")
    return event_id


def bottles_of(db, event_id):
    return [r["id"] for r in db.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position, id", (event_id,))]


def card(db, event_id, scores):
    """A form card keyed the way the page keys it: score-<event_wine_id>.

    `scores` is {which bottle, counting from zero: what to give it}.
    """
    ids = bottles_of(db, event_id)
    return {f"score-{ids[i]}": str(n) for i, n in scores.items()}


def votes_of(db, event_id, member="Morten"):
    return {r["name"]: r["score"] for r in db.query(
        """SELECT w.name, v.score FROM event_votes v
           JOIN event_wines w ON w.id = v.event_wine_id
           JOIN wine_members m ON m.id = v.member_id
           WHERE w.event_id=? AND m.name=? ORDER BY w.position""",
        (event_id, member))}


def as_member(client, name, db):
    member = db.query_one("SELECT id FROM wine_members WHERE name=?", (name,))
    client.post("/whoami", data={"member_id": member["id"]})
    return client


# -- when it is open --------------------------------------------------------


def test_an_evening_must_be_started_before_it_is_scored(signed_in, cellar):
    kinded(signed_in, "tasting", theme="Not yet", starts_at=tonight())
    event_id = latest(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Alfa"})
    response = signed_in.get(f"/events/{event_id}/vote")
    assert response.status_code == 409
    assert "has not started yet" in response.text


def test_starting_needs_a_bottle_to_score(signed_in, cellar):
    kinded(signed_in, "tasting", theme="Empty", starts_at=tonight())
    event_id = latest(cellar)["id"]
    response = signed_in.post(f"/events/{event_id}/start")
    assert response.status_code == 400
    assert "at least one bottle" in response.text
    assert cellar.query_one("SELECT started_at FROM events WHERE id=?", (event_id,))[0] is None


def test_starting_takes_you_to_the_scoring_page(signed_in, cellar):
    kinded(signed_in, "tasting", theme="Off we go", starts_at=tonight())
    event_id = latest(cellar)["id"]
    signed_in.post(f"/events/{event_id}/wines", data={"name": "Alfa"})
    response = signed_in.post(f"/events/{event_id}/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event_id}/vote"
    assert cellar.query_one("SELECT started_at FROM events WHERE id=?", (event_id,))[0]


def test_a_trip_is_not_scored(signed_in, cellar):
    kinded(signed_in, "trip", theme="Bilbao", country="Spain", starts_at=SOON)
    event_id = latest(cellar)["id"]
    assert signed_in.post(f"/events/{event_id}/start").status_code == 400
    assert signed_in.get(f"/events/{event_id}/vote").status_code == 409


def test_a_closed_evening_cannot_be_scored_again(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    signed_in.post(f"/events/{event_id}/close")
    response = signed_in.get(f"/events/{event_id}/vote")
    assert response.status_code == 409
    assert "in the cellar now" in response.text


# -- casting a card ---------------------------------------------------------


def test_a_card_lands_in_the_staging_table_and_nowhere_else(signed_in, cellar):
    """The whole point of event_votes: the cellar is untouched until close."""
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88, 1: 91}))
    assert votes_of(cellar, event_id) == {"Alfa": 88, "Beta": 91}
    assert cellar.query("SELECT * FROM wine_ratings WHERE wine_id > 3") == []
    assert cellar.query("SELECT * FROM wines WHERE name='Alfa'") == []


def test_a_wine_you_skipped_records_nothing(signed_in, cellar):
    """85 is marked, never pre-selected, so a blank box is a blank box."""
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88}))
    assert votes_of(cellar, event_id) == {"Alfa": 88}


def test_an_empty_card_records_nothing_at_all(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data={})
    assert votes_of(cellar, event_id) == {}


def test_submitting_again_replaces_your_own_card(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88, 1: 91}))
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 70}))
    # Beta is gone, not left at 91: clearing a box has to mean something.
    assert votes_of(cellar, event_id) == {"Alfa": 70}


def test_one_card_does_not_disturb_another(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88}))
    as_member(signed_in, "Tore", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 60}))
    assert votes_of(cellar, event_id, "Morten") == {"Alfa": 88}
    assert votes_of(cellar, event_id, "Tore") == {"Alfa": 60}


def test_the_typed_box_wins_over_the_dropdown(signed_in, cellar):
    """They are one number; the person who typed it meant it."""
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    ids = bottles_of(cellar, event_id)
    signed_in.post(f"/events/{event_id}/vote",
                   data={f"score-{ids[0]}": "85", f"typed-{ids[0]}": "93"})
    assert votes_of(cellar, event_id) == {"Alfa": 93}


@pytest.mark.parametrize("given, says", [("0", "not on the scale"),
                                         ("101", "not on the scale"),
                                         ("ninety", "not a score")])
def test_a_score_off_the_scale_is_refused(signed_in, cellar, given, says):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    ids = bottles_of(cellar, event_id)
    response = signed_in.post(
        f"/events/{event_id}/vote",
        data={f"score-{ids[0]}": "88", f"score-{ids[1]}": given},
    )
    assert response.status_code == 400
    assert says in response.text
    assert votes_of(cellar, event_id) == {}, "nothing is written when part of it is wrong"
    assert 'value="88"' in response.text, "and the rest of the card is still there"


def test_you_have_to_say_who_you_are_before_you_score(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    response = signed_in.post(f"/events/{event_id}/vote",
                              data=card(cellar, event_id, {0: 88}))
    assert response.status_code == 400
    assert "who you are" in response.text
    assert cellar.query("SELECT * FROM event_votes") == []


def test_the_page_says_who_has_submitted_and_no_more(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 93}))
    page = signed_in.get(f"/events/{event_id}/vote").text
    submitted = page.split("<h2>Submitted</h2>")[1]
    assert "Morten" in submitted
    assert "93" not in submitted, "a score before the reveal is the one thing forbidden"


# -- blind ------------------------------------------------------------------


def test_a_blind_evening_is_scored_without_names(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar, kind="blind",
                             bottles=("Vega Sicilia Unico", "Pingus Ribera"))
    as_member(signed_in, "Morten", cellar)
    page = signed_in.get(f"/events/{event_id}/vote").text
    assert "Vega Sicilia Unico" not in page
    assert "Pingus Ribera" not in page
    assert "Wine 1" in page and "Wine 2" in page


# -- closing ----------------------------------------------------------------


def test_closing_carries_the_scores_into_the_cellar(signed_in, cellar):
    """What the evening was for: staging tables to fifteen years of history."""
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88, 1: 91}))
    as_member(signed_in, "Tore", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 84}))

    signed_in.post(f"/events/{event_id}/close")
    tasting_id = cellar.query_one("SELECT tasting_id FROM events WHERE id=?", (event_id,))[0]
    scored = cellar.query(
        """SELECT w.name, m.name AS who, r.score FROM wine_ratings r
           JOIN wines w ON w.id = r.wine_id JOIN wine_members m ON m.id = r.member_id
           WHERE w.tasting_id=? ORDER BY w.id, m.name""",
        (tasting_id,),
    )
    assert [(r["name"], r["who"], r["score"]) for r in scored] == [
        ("Alfa", "Morten", 88), ("Alfa", "Tore", 84), ("Beta", "Morten", 91),
    ]
    page = signed_in.get(f"/wine/tastings/{tasting_id}").text
    assert "86.0" in page, "Alfa's average, on the tasting page"


def test_closing_twice_changes_nothing(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar)
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 88}))
    signed_in.post(f"/events/{event_id}/close")
    before = cellar.query_one("SELECT COUNT(*) n FROM wine_ratings")["n"]
    signed_in.post(f"/events/{event_id}/close")
    assert cellar.query_one("SELECT COUNT(*) n FROM wine_ratings")["n"] == before


def test_a_blind_evening_is_named_when_it_closes(signed_in, cellar):
    event_id = ready_to_vote(signed_in, cellar, kind="blind",
                             bottles=("Vega Sicilia Unico",))
    as_member(signed_in, "Morten", cellar)
    signed_in.post(f"/events/{event_id}/vote", data=card(cellar, event_id, {0: 95}))
    signed_in.post(f"/events/{event_id}/close")
    tasting_id = cellar.query_one("SELECT tasting_id FROM events WHERE id=?", (event_id,))[0]
    page = signed_in.get(f"/wine/tastings/{tasting_id}").text
    assert "Vega Sicilia Unico" in page and "95" in page


# -- an evening cannot start before its own day -----------------------------
#
# Starting opens the scoring page and does not come back, so a stray click on
# next January's row in the diary must not be able to do it. Running early on
# the night itself is normal, though — the club sits down at seven whatever the
# diary says — so the gate is the calendar day, not the hour.


def with_bottle(client, db, when, theme="Dated"):
    kinded(client, "tasting", theme=theme, starts_at=when)
    event_id = latest(db)["id"]
    client.post(f"/events/{event_id}/wines", data={"name": "Alfa"})
    return event_id


def started(db, event_id):
    return db.query_one("SELECT started_at FROM events WHERE id=?", (event_id,))[0]


def test_tomorrows_evening_cannot_be_started_today(signed_in, cellar):
    tomorrow = (datetime.now(ZoneInfo("Europe/Oslo")).date() + timedelta(days=1))
    event_id = with_bottle(signed_in, cellar, f"{tomorrow.isoformat()}T19:00")

    page = signed_in.get(f"/events/{event_id}").text
    assert "disabled" in page.split("Start event")[0][-120:], "the button is greyed"
    assert "you can start it on" in page.lower(), "and the page says when"

    response = signed_in.post(f"/events/{event_id}/start")
    assert response.status_code == 400
    assert "Too early" in response.text
    assert started(cellar, event_id) is None


def test_todays_evening_starts_however_early_in_the_day(signed_in, cellar):
    """Nine in the morning for an evening at seven: fine, it is the day."""
    event_id = with_bottle(signed_in, cellar, tonight("23:30"))
    page = signed_in.get(f"/events/{event_id}").text
    assert "disabled" not in page.split("Start event")[0][-120:]
    response = signed_in.post(f"/events/{event_id}/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event_id}/vote"
    assert started(cellar, event_id)


def test_an_evening_whose_day_has_passed_still_starts(signed_in, cellar):
    """Late is not too early. The rule is a floor, not a window."""
    event_id = with_bottle(signed_in, cellar, GONE)
    assert signed_in.post(f"/events/{event_id}/start",
                          follow_redirects=False).status_code == 303
    assert started(cellar, event_id)


# -- the boundary the whole rule turns on -----------------------------------


@pytest.mark.parametrize("starts_at, now, expected", [
    # 00:30 on the 7th in Oslo is 22:30 on the 6th in UTC. Compare the stored
    # instants and this evening opens a day early; compare local dates and it
    # does not. This is the test that fails if the timezone is dropped.
    ("2026-10-06T22:30:00+00:00", "2026-10-06T20:00:00+00:00", False),
    # The same evening, once its own day has actually begun locally.
    ("2026-10-06T22:30:00+00:00", "2026-10-06T22:05:00+00:00", True),
    # 23:30 on the 6th in Oslo, checked at teatime that day.
    ("2026-10-06T21:30:00+00:00", "2026-10-06T14:00:00+00:00", True),
    ("2026-10-08T17:00:00+00:00", "2026-10-06T14:00:00+00:00", False),
])
def test_the_day_is_the_clubs_day_not_utcs(starts_at, now, expected):
    from datetime import datetime as dt

    from web.routes.events import _day_has_come

    assert _day_has_come({"starts_at": starts_at}, ZoneInfo("Europe/Oslo"),
                         dt.fromisoformat(now)) is expected


def test_a_date_nobody_can_read_does_not_lock_the_evening_shut():
    """It cannot happen. If it ever did, refusing would be the worse failure."""
    from web.routes.events import _day_has_come

    assert _day_has_come({"starts_at": "not a date"}, ZoneInfo("Europe/Oslo")) is True


# -- whose score, on the page ----------------------------------------------
#
# The cellar fixture: three wines, scored by Andy, Morten and Tore in that
# order. Ch. Musar is Tore's 91 against a 92.3 average; Boroli is his 78
# against 80.0 — so "what does Tore like" and "what did the room like" give
# different answers, which is the whole reason for the filter.


def test_the_scored_by_dropdown_offers_everyone_who_rated(signed_in):
    page = signed_in.get("/wine").text
    picker = page.split('name="rated_by"')[1].split("</select>")[0]
    for name in ("Andy", "Morten", "Tore"):
        assert f"{name} (3)" in picker, "with a count, like the other filters"


def test_picking_a_member_shows_their_score_beside_the_average(signed_in):
    rows = signed_in.get("/wine/results", params={**BLANK_FILTERS, "rated_by": "Tore"}).text
    assert '<th class="num">Tore</th>' in rows, "a column headed with their name"
    assert '<th class="num">Average</th>' in rows, "and the room's, to compare"
    musar = rows.split("Ch. Musar 2005")[1][:260]
    assert ">91<" in musar, "Tore's own score"
    assert "92.3" in musar, "and what everyone made of it"


def test_the_table_is_untouched_when_nobody_is_picked(signed_in):
    rows = signed_in.get("/wine/results", params=BLANK_FILTERS).text
    assert '<th class="num">Score</th>' in rows
    assert '<th class="num">Average</th>' not in rows


def test_the_minimum_follows_the_member_you_picked(signed_in):
    """Tore gave Ch. Musar 91 and Massolino 86; the room gave them 92.3 and 88."""
    his = signed_in.get("/wine/results",
                        params={**BLANK_FILTERS, "rated_by": "Tore", "min_score": "90"}).text
    assert "Ch. Musar 2005" in his
    assert "Massolino Barolo 2015" not in his, "he gave it 86, whatever the room said"

    anyones = signed_in.get("/wine/results",
                            params={**BLANK_FILTERS, "min_score": "90"}).text
    assert "Ch. Musar 2005" in anyones
    assert "Massolino" not in anyones


def test_a_member_who_missed_an_evening_does_not_see_that_wine(signed_in, cellar):
    """Andy was not at the Musar evening, so it is not his to have an opinion on."""
    cellar.execute("DELETE FROM wine_ratings WHERE wine_id=3 AND member_id=1")
    rows = signed_in.get("/wine/results", params={**BLANK_FILTERS, "rated_by": "Andy"}).text
    assert "Ch. Musar 2005" not in rows
    assert "Massolino Barolo 2015" in rows


def test_the_label_says_whose_minimum_it_is(signed_in):
    page = signed_in.get("/wine", params={**BLANK_FILTERS, "rated_by": "Tore"}).text
    assert "Tore scored at least" in page


def test_a_changed_score_is_noticed(signed_in, cellar):
    """The index is rebuilt on a fingerprint of counts and timestamps. A score
    that is *edited* moves neither count — and a card resubmitted during an
    evening does exactly that — so `MAX(rated_at)` is what catches it."""
    before = signed_in.get("/wine/results",
                           params={**BLANK_FILTERS, "rated_by": "Tore"}).text
    assert ">91<" in before.split("Ch. Musar 2005")[1][:260]

    cellar.execute(
        "UPDATE wine_ratings SET score=60, rated_at=? WHERE wine_id=3 AND member_id=3",
        ("2027-01-01T12:00:00+00:00",),
    )
    after = signed_in.get("/wine/results",
                          params={**BLANK_FILTERS, "rated_by": "Tore"}).text
    assert ">60<" in after.split("Ch. Musar 2005")[1][:260]


# -- the filters have to reach the server to filter -------------------------


def test_every_control_can_trigger_the_search(signed_in):
    """`from:find select` resolves to *one* element in htmx, so scoping the
    triggers that way listened to the search box and the Country dropdown and
    to nothing else — eight of the nine filters did nothing unless you pressed
    Enter. The triggers belong on the form, where bubbling reaches them all."""
    # By class, not position: the chrome's "who are you?" form comes first.
    form = signed_in.get("/wine").text.split('<form class="card search"', 1)[1]
    attributes = form.split(">", 1)[0]
    assert "from:find" not in attributes, "scoping a trigger to one control kills the rest"
    assert "change" in attributes, "dropdowns fire change"
    assert "input changed" in attributes, "and typing is still debounced"


def test_the_search_form_has_the_controls_those_triggers_cover(signed_in):
    """If a filter is added later it is covered by the form-level triggers, so
    this only has to notice that they are all inside the one form."""
    page = signed_in.get("/wine").text
    form = page.split('<form class="card search"', 1)[1].split("</form>", 1)[0]
    for name in ("q", "country", "region", "grape", "brought_by", "rated_by",
                 "year_from", "year_to", "vintage_from", "vintage_to", "min_score"):
        assert f'name="{name}"' in form, f"{name} is outside the form that listens"


def test_a_missing_sdk_reads_as_a_sentence(monkeypatch):
    """A key set and `anthropic` not installed is a real state — it is the
    machine the club runs the app on today. `sys.modules[name] = None` is what
    makes `import anthropic` fail the way it would there."""
    import sys

    from web.label import LabelUnreadable, read_label

    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LabelUnreadable, match="isn't installed"):
        read_label(b"a photo", "image/jpeg", "a-key")


def test_a_missing_sdk_leaves_the_bottle_addable_by_hand(with_camera, evening, monkeypatch):
    """The promise the whole feature rests on: it comes back as a page."""
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)
    response = shoot(with_camera, evening)
    assert response.status_code == 400, "a sentence, not a stack trace"
    assert "isn&#39;t installed" in response.text
    assert 'name="name"' in response.text, "and the form is still there"


# -- correcting a bottle, and changing the running order --------------------


def lineup(db, event_id):
    return [r["name"] for r in db.query(
        "SELECT name FROM event_wines WHERE event_id=? ORDER BY position, id", (event_id,))]


def three_bottles(client, db, kind="tasting"):
    kinded(client, kind, theme=f"Lineup {kind}", starts_at=tonight())
    event_id = latest(db)["id"]
    for name in ("Alfa", "Beta", "Gamma"):
        client.post(f"/events/{event_id}/wines", data={"name": name})
    return event_id


def test_a_bottle_can_be_corrected_after_it_is_added(signed_in, cellar):
    event_id = three_bottles(signed_in, cellar)
    wine_id = cellar.query("SELECT id FROM event_wines WHERE event_id=? ORDER BY position",
                           (event_id,))[0]["id"]
    signed_in.post(f"/events/{event_id}/wines/{wine_id}",
                   data={"name": "Alfa Romeo", "vintage": "2019", "country": "Italy"})
    row = cellar.query_one("SELECT * FROM event_wines WHERE id=?", (wine_id,))
    assert (row["name"], row["vintage"], row["country"]) == ("Alfa Romeo", 2019, "Italy")
    assert lineup(cellar, event_id) == ["Alfa Romeo", "Beta", "Gamma"], "and it stays put"


def test_a_correction_needs_a_name(signed_in, cellar):
    event_id = three_bottles(signed_in, cellar)
    wine_id = cellar.query_one("SELECT id FROM event_wines WHERE event_id=?", (event_id,))["id"]
    response = signed_in.post(f"/events/{event_id}/wines/{wine_id}", data={"name": " "})
    assert response.status_code == 400
    assert cellar.query_one("SELECT name FROM event_wines WHERE id=?", (wine_id,))["name"] == "Alfa"


def test_a_blind_evening_is_not_editable_until_it_closes(signed_in, cellar):
    """A form prefilled with the name would undo the one rule that makes it
    blind, so neither the page nor the route offers it."""
    event_id = three_bottles(signed_in, cellar, kind="blind")
    wine_id = cellar.query_one("SELECT id FROM event_wines WHERE event_id=?", (event_id,))["id"]
    page = signed_in.get(f"/events/{event_id}").text
    assert ">Edit<" not in page.split("The wines")[1].split("Add a bottle")[0]

    response = signed_in.post(f"/events/{event_id}/wines/{wine_id}", data={"name": "Revealed"})
    assert response.status_code == 400
    assert "named when it closes" in response.text
    assert cellar.query_one("SELECT name FROM event_wines WHERE id=?", (wine_id,))["name"] == "Alfa"


# -- the running order ------------------------------------------------------


def test_the_bottles_can_be_put_in_another_order(signed_in, cellar):
    event_id = three_bottles(signed_in, cellar)
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]
    signed_in.post(f"/events/{event_id}/wines/order",
                   data={"order": f"{ids[2]},{ids[0]},{ids[1]}"})
    assert lineup(cellar, event_id) == ["Gamma", "Alfa", "Beta"]


def test_one_step_at_a_time_without_any_javascript(signed_in, cellar):
    """The ▲▼ buttons are the mechanism; the drag only posts the same order."""
    event_id = three_bottles(signed_in, cellar)
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]
    signed_in.post(f"/events/{event_id}/wines/{ids[2]}/move", data={"direction": "up"})
    assert lineup(cellar, event_id) == ["Alfa", "Gamma", "Beta"]
    signed_in.post(f"/events/{event_id}/wines/{ids[0]}/move", data={"direction": "down"})
    assert lineup(cellar, event_id) == ["Gamma", "Alfa", "Beta"]


def test_moving_past_the_end_does_nothing(signed_in, cellar):
    event_id = three_bottles(signed_in, cellar)
    first = cellar.query("SELECT id FROM event_wines WHERE event_id=? ORDER BY position",
                         (event_id,))[0]["id"]
    signed_in.post(f"/events/{event_id}/wines/{first}/move", data={"direction": "up"})
    assert lineup(cellar, event_id) == ["Alfa", "Beta", "Gamma"]


def test_a_stray_id_in_a_posted_order_is_ignored(signed_in, cellar):
    """Not trusted, and not a crash: the evening keeps all three bottles."""
    event_id = three_bottles(signed_in, cellar)
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]
    signed_in.post(f"/events/{event_id}/wines/order", data={"order": f"9999,{ids[1]},abc"})
    assert sorted(lineup(cellar, event_id)) == ["Alfa", "Beta", "Gamma"]
    assert lineup(cellar, event_id)[0] == "Beta", "the one real id led"


def test_the_order_is_fixed_once_anybody_is_scoring(signed_in, cellar):
    """Renumbering Wine 1…N mid-card would move the bottles under people. The
    votes would stay right, which is what makes it silent and worth refusing."""
    event_id = three_bottles(signed_in, cellar, kind="blind")
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]
    signed_in.post(f"/events/{event_id}/start")

    assert ">⠿<" not in signed_in.get(f"/events/{event_id}").text, "no handle either"
    shuffled = {"order": f"{ids[2]},{ids[0]},{ids[1]}"}
    for path, data in ((f"/events/{event_id}/wines/order", shuffled),
                       (f"/events/{event_id}/wines/{ids[0]}/move", {"direction": "down"})):
        response = signed_in.post(path, data=data)
        assert response.status_code == 409
        assert "running order is fixed" in response.text
    assert lineup(cellar, event_id) == ["Alfa", "Beta", "Gamma"]


def test_the_cellar_keeps_the_order_you_left_it_in(signed_in, cellar):
    """What reordering is for: it is the order the archive ends up holding."""
    event_id = three_bottles(signed_in, cellar)
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]
    signed_in.post(f"/events/{event_id}/wines/order",
                   data={"order": f"{ids[2]},{ids[1]},{ids[0]}"})
    signed_in.post(f"/events/{event_id}/close")
    tasting_id = cellar.query_one("SELECT tasting_id FROM events WHERE id=?", (event_id,))[0]
    assert [r["name"] for r in cellar.query(
        "SELECT name FROM wines WHERE tasting_id=? ORDER BY id", (tasting_id,))] == [
        "Gamma", "Beta", "Alfa"]


def test_the_camera_button_appears_only_with_a_key(with_camera, evening, signed_in, cellar):
    """The script behind it cannot be tested here — there is no browser on this
    machine — but whether the control is rendered at all can be."""
    assert "Use the camera" in with_camera.get(f"/events/{evening}").text

    make_event(signed_in, theme="No camera")
    page = signed_in.get(f"/events/{latest(cellar)['id']}").text
    assert "Use the camera" not in page
    assert "Photograph the label" not in page


def test_the_camera_hands_its_picture_to_the_upload_that_already_works(with_camera, evening):
    """One path to the reader, not two: the capture fills the file input and
    the ordinary form posts it, so there is no second way for a read to fail."""
    page = with_camera.get(f"/events/{evening}").text
    assert "getUserMedia" in page
    assert "new DataTransfer()" in page
    assert "requestSubmit" in page
    assert page.count(f'action="/events/{evening}/wines/label"') == 1


def test_each_bottle_is_one_row_with_its_form_marked_as_following(signed_in, cellar):
    """The drag moves a bottle and its Edit form together, and finds the form
    by `data-follows`. If that marker or the row order ever drifts, a drag
    leaves a form stranded behind somebody else's bottle."""
    event_id = three_bottles(signed_in, cellar)
    page = signed_in.get(f"/events/{event_id}").text
    lineup = page.split('id="lineup"')[1].split("</tbody>")[0]
    ids = [r["id"] for r in cellar.query(
        "SELECT id FROM event_wines WHERE event_id=? ORDER BY position", (event_id,))]

    assert lineup.count("data-wine=") == 3
    assert lineup.count("data-follows=") == 3
    for wine_id in ids:
        bottle = lineup.index(f'data-wine="{wine_id}"')
        form = lineup.index(f'data-follows="{wine_id}"')
        assert form > bottle, "a form comes after its own bottle"
        # And nothing else between them: the next data-wine is further on.
        following = lineup.find("data-wine=", bottle + 1)
        assert following == -1 or form < following


def test_escape_and_the_edit_toggle_are_wired_to_the_panels(signed_in, cellar):
    """The behaviour is the browser's; what can be held here is that the page
    carries the hooks the script binds to."""
    event_id = three_bottles(signed_in, cellar)
    page = signed_in.get(f"/events/{event_id}").text
    assert "data-edits=" in page, "the Edit buttons are bound by the script, not inline"
    assert "'Escape'" in page
    assert "onclick=\"document.getElementById" not in page, "no inline handler left behind"
