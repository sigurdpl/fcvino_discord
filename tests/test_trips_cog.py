"""The trips cog's database side: upserts, resolution, autocomplete, rendering."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.cogs.trips import Trips, parse_day


@pytest.fixture()
def cog(db):
    instance = Trips.__new__(Trips)
    instance.bot = SimpleNamespace(db=db)
    return instance


def add_trip(db, year, country, city=None, added_by=1):
    return db.upsert_trip(year=year, country=country, city=city, added_by=added_by)


def add_match(db, trip_id, home, away, hg=None, ag=None, **extra):
    return db.execute(
        """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                     home_goals, away_goals, city, stadium, attendance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trip_id,
            extra.get("match_date"),
            extra.get("competition"),
            home,
            away,
            hg,
            ag,
            extra.get("city"),
            extra.get("stadium"),
            extra.get("attendance"),
        ),
    )


# -- date validation --------------------------------------------------------


def test_parse_day_accepts_an_iso_date():
    assert parse_day("2014-04-26") == ("2014-04-26", None)


def test_parse_day_treats_blank_as_simply_absent():
    assert parse_day(None) == (None, None)
    assert parse_day("   ") == (None, None)


@pytest.mark.parametrize("bad", ["26/04/2014", "April 2014", "2014-13-01", "2014-04-31"])
def test_parse_day_rejects_what_it_cannot_read(bad):
    value, error = parse_day(bad)
    assert value is None
    assert error is not None and "YYYY-MM-DD" in error


# -- trips ------------------------------------------------------------------


def test_all_trips_is_ordered_by_year(cog, db):
    add_trip(db, 2016, "Italy")
    add_trip(db, 2010, "England")
    assert [t["year"] for t in cog._all_trips()] == [2010, 2016]


def test_a_year_holds_one_trip_and_re_adding_corrects_the_country(cog, db):
    # We go one place a year, so 2019 cannot be both Spain and Portugal.
    first = add_trip(db, 2019, "Spain")
    second = add_trip(db, 2019, "Portugal")
    assert first == second
    trips = cog._all_trips()
    assert len(trips) == 1 and trips[0]["country"] == "Portugal"


def test_a_trip_is_found_by_its_year(cog, db):
    trip_id = add_trip(db, 2014, "Germany")
    found = cog._trip_for_year(2014)
    assert found is not None and found["id"] == trip_id


def test_a_year_with_no_trip_resolves_to_nothing(cog, db):
    add_trip(db, 2014, "Germany")
    assert cog._trip_for_year(2013) is None


# -- matches and goals ------------------------------------------------------


def test_trip_matches_are_scoped_to_their_trip(cog, db):
    germany = add_trip(db, 2014, "Germany")
    italy = add_trip(db, 2016, "Italy")
    add_match(db, germany, "Dortmund", "Bayern")
    add_match(db, italy, "Roma", "Lazio")
    assert [m["home"] for m in cog._trip_matches(germany)] == ["Dortmund"]


def test_resolve_match_by_team_name(cog, db):
    trip_id = add_trip(db, 2016, "Italy")
    match_id = add_match(db, trip_id, "Roma", "Lazio", 4, 3)
    assert cog._resolve_match("lazio")["id"] == match_id


def test_goals_are_ordered_by_minute(cog, db):
    trip_id = add_trip(db, 2014, "Germany")
    match_id = add_match(db, trip_id, "Dortmund", "Bayern", 0, 3)
    for minute, scorer in ((67, "Goetze"), (23, "Robben")):
        db.execute(
            "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) "
            "VALUES (?, ?, ?, 'A')",
            (match_id, minute, scorer),
        )
    assert [g["scorer"] for g in cog._goals(match_id)] == ["Robben", "Goetze"]


def test_removing_a_trip_takes_its_matches_and_goals(cog, db):
    trip_id = add_trip(db, 2014, "Germany")
    match_id = add_match(db, trip_id, "Dortmund", "Bayern", 0, 3)
    db.execute(
        "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) "
        "VALUES (?, 23, 'Robben', 'A')",
        (match_id,),
    )
    db.execute("DELETE FROM trips WHERE id=?", (trip_id,))
    assert db.query("SELECT * FROM trip_matches") == []
    assert db.query("SELECT * FROM trip_goals") == []


# -- autocomplete -----------------------------------------------------------


def test_country_autocomplete_offers_existing_countries_only_once(cog, db):
    add_trip(db, 2010, "England")
    add_trip(db, 2012, "England")
    add_trip(db, 2014, "Germany")
    choices = asyncio.run(cog.country_autocomplete(None, ""))
    assert [c.value for c in choices] == ["England", "Germany"]


def test_country_autocomplete_filters_on_what_is_typed(cog, db):
    add_trip(db, 2010, "England")
    add_trip(db, 2014, "Germany")
    choices = asyncio.run(cog.country_autocomplete(None, "ger"))
    assert [c.value for c in choices] == ["Germany"]


def test_year_autocomplete_shows_newest_first_and_carries_the_year(cog, db):
    add_trip(db, 2010, "England")
    add_trip(db, 2014, "Germany", city="Dortmund")
    choices = asyncio.run(cog.year_autocomplete(None, ""))
    assert [c.value for c in choices] == [2014, 2010]
    assert "Dortmund" in choices[0].name


def test_match_autocomplete_labels_with_the_year_and_score(cog, db):
    trip_id = add_trip(db, 2016, "Italy")
    add_match(db, trip_id, "Roma", "Lazio", 4, 3)
    choices = asyncio.run(cog.match_autocomplete(None, "roma"))
    assert len(choices) == 1
    assert choices[0].name.startswith("2016 · Roma 4–3 Lazio")


def test_match_autocomplete_distinguishes_two_matches_on_one_trip(cog, db):
    trip_id = add_trip(db, 2014, "Germany", city="Dortmund")
    add_match(db, trip_id, "Dortmund", "Bayern", 0, 3, match_date="2014-04-26")
    add_match(db, trip_id, "Schalke", "Koeln", 1, 1, match_date="2014-04-27",
              city="Gelsenkirchen")
    labels = [c.name for c in asyncio.run(cog.match_autocomplete(None, ""))]
    assert len(labels) == 2
    assert len(set(labels)) == 2, "two games on one trip must be tellable apart"
    assert any("Gelsenkirchen" in label for label in labels)
    assert all("2014-04-2" in label for label in labels)


def test_autocompletes_are_empty_rather_than_broken_on_a_fresh_database(cog):
    assert asyncio.run(cog.country_autocomplete(None, "")) == []
    assert asyncio.run(cog.year_autocomplete(None, "")) == []
    assert asyncio.run(cog.match_autocomplete(None, "")) == []


# -- rendering --------------------------------------------------------------


def test_fixture_text_shows_a_dash_when_the_score_is_unknown(cog, db):
    trip_id = add_trip(db, 2018, "Scotland")
    add_match(db, trip_id, "Celtic", "Rangers")
    match = cog._trip_matches(trip_id)[0]
    assert cog._fixture_text(match) == "Celtic **–** Rangers"


def test_trip_embed_says_so_when_no_match_is_recorded(cog, db):
    trip_id = add_trip(db, 2018, "Scotland")
    e = cog._trip_embed(db.query_one("SELECT * FROM trips WHERE id=?", (trip_id,)))
    assert "Not recorded yet" in e.fields[-1].value


def test_full_trip_embed_includes_scorers_and_the_ground(cog, db):
    trip_id = add_trip(db, 2014, "Germany", city="Dortmund")
    match_id = add_match(
        db,
        trip_id,
        "Dortmund",
        "Bayern",
        0,
        3,
        stadium="Signal Iduna Park",
        attendance=80667,
        competition="Bundesliga",
    )
    db.execute(
        "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) "
        "VALUES (?, 23, 'Robben', 'A')",
        (match_id,),
    )
    trip = db.query_one("SELECT * FROM trips WHERE id=?", (trip_id,))
    e = cog._trip_embed(trip, full=True)
    assert "2014" in e.title and "Dortmund" in e.title
    body = e.fields[-1].value
    assert "Signal Iduna Park" in body
    assert "80 667 in" in body
    assert "23' Robben (A)" in body


def test_summary_embed_leaves_scorers_out(cog, db):
    trip_id = add_trip(db, 2014, "Germany")
    match_id = add_match(db, trip_id, "Dortmund", "Bayern", 0, 3)
    db.execute(
        "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) "
        "VALUES (?, 23, 'Robben', 'A')",
        (match_id,),
    )
    trip = db.query_one("SELECT * FROM trips WHERE id=?", (trip_id,))
    assert "Robben" not in cog._trip_embed(trip).fields[-1].value


# -- the joined row shape the stats functions rely on -----------------------


def test_trip_match_rows_join_the_year_and_country_on(db):
    trip_id = add_trip(db, 2014, "Germany", city="Dortmund")
    add_match(db, trip_id, "Dortmund", "Bayern", 0, 3)
    row = db.trip_match_rows()[0]
    assert (row["year"], row["country"], row["trip_city"]) == (2014, "Germany", "Dortmund")


def test_a_match_without_its_own_city_inherits_the_trips(db):
    trip_id = add_trip(db, 2015, "England", city="London")
    add_match(db, trip_id, "Arsenal", "Chelsea", 1, 0)
    row = db.trip_match_rows()[0]
    assert row["city"] == "London"
    assert row["match_city"] is None, "the fallback must stay distinguishable"


def test_a_match_with_its_own_city_keeps_it(db):
    trip_id = add_trip(db, 2014, "Germany", city="Dortmund")
    add_match(db, trip_id, "Schalke", "Koeln", 1, 1, city="Gelsenkirchen")
    row = db.trip_match_rows()[0]
    assert (row["city"], row["match_city"]) == ("Gelsenkirchen", "Gelsenkirchen")


def test_trip_match_rows_can_be_scoped_to_one_trip(db):
    germany = add_trip(db, 2014, "Germany")
    italy = add_trip(db, 2016, "Italy")
    add_match(db, germany, "Dortmund", "Bayern")
    add_match(db, italy, "Roma", "Lazio")
    assert [r["home"] for r in db.trip_match_rows(trip_id=italy)] == ["Roma"]


def test_trip_match_rows_are_ordered_chronologically(db):
    late = add_trip(db, 2016, "Italy")
    early = add_trip(db, 2014, "Germany")
    add_match(db, late, "Roma", "Lazio")
    add_match(db, early, "Dortmund", "Bayern")
    assert [r["year"] for r in db.trip_match_rows()] == [2014, 2016]
