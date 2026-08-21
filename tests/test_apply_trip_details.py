"""The research pass: seeds/trip_details.json -> the database.

The contract that matters is "safe to re-run, and never clobbers what a human
typed". Both are asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.apply_trip_details import DEFAULT_SEED, apply_seed


def seed(**overrides):
    entry = {
        "year": 2014,
        "country": "Germany",
        "city": "Dortmund",
        "date_from": "2014-04-25",
        "matches": [
            {
                "match_date": "2014-04-26",
                "competition": "Bundesliga",
                "home": "Borussia Dortmund",
                "away": "Bayern Munich",
                "home_goals": 0,
                "away_goals": 3,
                "stadium": "Signal Iduna Park",
                "attendance": 80667,
                "goals": ["23 Robben A", "45 Mueller A"],
            }
        ],
    }
    entry.update(overrides)
    return {"trips": [entry]}


def only_trip(db):
    return db.query_one("SELECT * FROM trips")


def only_match(db):
    return db.query_one("SELECT * FROM trip_matches")


# -- creating ---------------------------------------------------------------


def test_without_create_an_unknown_trip_is_reported_not_invented(db):
    report = apply_seed(db, seed())
    assert report.changes == 0
    assert db.query("SELECT * FROM trips") == []
    assert any("no such trip" in p for p in report.problems)


def test_create_inserts_the_trip_the_match_and_the_scorers(db):
    apply_seed(db, seed(), create=True)
    trip = only_trip(db)
    match = only_match(db)
    assert (trip["year"], trip["country"], trip["city"]) == (2014, "Germany", "Dortmund")
    assert (match["home_goals"], match["away_goals"]) == (0, 3)
    assert match["stadium"] == "Signal Iduna Park"
    assert [g["scorer"] for g in db.query("SELECT * FROM trip_goals ORDER BY minute")] == [
        "Robben",
        "Mueller",
    ]


def test_country_is_normalised_on_the_way_in(db):
    apply_seed(db, seed(country="germany"), create=True)
    assert only_trip(db)["country"] == "Germany"


def test_a_seeded_row_is_marked_as_not_belonging_to_a_person(db):
    apply_seed(db, seed(), create=True)
    assert only_trip(db)["added_by"] == 0


# -- idempotency ------------------------------------------------------------


def test_a_second_pass_changes_nothing(db):
    apply_seed(db, seed(), create=True)
    second = apply_seed(db, seed(), create=True)
    assert second.changes == 0
    assert second.problems == []


def test_a_second_pass_does_not_duplicate_matches_or_goals(db):
    apply_seed(db, seed(), create=True)
    apply_seed(db, seed(), create=True)
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_matches")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_goals")["n"] == 2


# -- not clobbering people --------------------------------------------------


def test_what_a_human_typed_wins_by_default(db):
    apply_seed(db, seed(), create=True)
    db.execute("UPDATE trip_matches SET stadium='Westfalenstadion'")
    report = apply_seed(db, seed())
    assert only_match(db)["stadium"] == "Westfalenstadion"
    assert any("keeping stadium" in line for line in report.lines)


def test_overwrite_lets_the_file_win(db):
    apply_seed(db, seed(), create=True)
    db.execute("UPDATE trip_matches SET stadium='Westfalenstadion'")
    apply_seed(db, seed(), overwrite=True)
    assert only_match(db)["stadium"] == "Signal Iduna Park"


def test_empty_columns_are_filled_without_overwrite(db):
    apply_seed(db, seed(), create=True)
    db.execute("UPDATE trip_matches SET attendance=NULL, competition=NULL")
    apply_seed(db, seed())
    match = only_match(db)
    assert (match["attendance"], match["competition"]) == (80667, "Bundesliga")


def test_existing_scorers_are_left_alone_unless_overwriting(db):
    apply_seed(db, seed(), create=True)
    db.execute("DELETE FROM trip_goals")
    db.execute(
        """INSERT INTO trip_goals (trip_match_id, minute, scorer, side)
           SELECT id, 90, 'Someone Else', 'H' FROM trip_matches"""
    )
    apply_seed(db, seed())
    assert [g["scorer"] for g in db.query("SELECT * FROM trip_goals")] == ["Someone Else"]

    apply_seed(db, seed(), overwrite=True)
    assert [g["scorer"] for g in db.query("SELECT * FROM trip_goals ORDER BY minute")] == [
        "Robben",
        "Mueller",
    ]


def test_a_zero_score_is_treated_as_present_not_missing(db):
    # 0 is falsy; the fill logic must not mistake a 0-0 for an empty column.
    payload = seed()
    payload["trips"][0]["matches"][0]["home_goals"] = 0
    payload["trips"][0]["matches"][0]["away_goals"] = 0
    apply_seed(db, payload, create=True)
    db.execute("UPDATE trip_matches SET home_goals=1, away_goals=1")
    apply_seed(db, payload)
    match = only_match(db)
    assert (match["home_goals"], match["away_goals"]) == (1, 1)


# -- matching ---------------------------------------------------------------


def test_a_match_is_found_by_team_names(db):
    apply_seed(db, seed(), create=True)
    db.execute("UPDATE trip_matches SET stadium=NULL, home='borussia dortmund'")
    apply_seed(db, seed())
    assert only_match(db)["stadium"] == "Signal Iduna Park", "team matching is case-insensitive"


def test_the_only_match_on_a_trip_is_matched_even_if_named_differently(db):
    apply_seed(db, seed(), create=True)
    db.execute("UPDATE trip_matches SET home='BVB', away='FCB', stadium=NULL, match_date=NULL")
    apply_seed(db, seed())
    assert only_match(db)["stadium"] == "Signal Iduna Park"


def test_an_extra_fixture_is_reported_rather_than_guessed(db):
    apply_seed(db, seed(), create=True)
    db.execute(
        """INSERT INTO trip_matches (trip_id, home, away)
           SELECT id, 'Schalke', 'Koln' FROM trips"""
    )
    payload = seed()
    payload["trips"][0]["matches"][0]["home"] = "Someone"
    payload["trips"][0]["matches"][0]["away"] = "Unknown"
    payload["trips"][0]["matches"][0]["match_date"] = None
    report = apply_seed(db, payload)
    assert any("no matching fixture" in p for p in report.problems)


# -- input handling ---------------------------------------------------------


def test_a_bad_goal_string_is_reported_and_the_rest_still_land(db):
    payload = seed()
    payload["trips"][0]["matches"][0]["goals"] = ["23 Robben A", "sometime later, Mueller"]
    report = apply_seed(db, payload, create=True)
    assert any("could not parse goal" in p for p in report.problems)
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_goals")["n"] >= 1


def test_an_entry_with_no_year_is_rejected(db):
    report = apply_seed(db, {"trips": [{"country": "Germany"}]}, create=True)
    assert any("no year/country" in p for p in report.problems)
    assert db.query("SELECT * FROM trips") == []


def test_dry_run_writes_nothing(db):
    report = apply_seed(db, seed(), create=True, dry_run=True)
    assert report.changes > 0
    assert db.query("SELECT * FROM trips") == []


def test_an_empty_payload_is_a_no_op(db):
    report = apply_seed(db, {"trips": []}, create=True)
    assert (report.changes, report.problems) == (0, [])


# -- the shipped file -------------------------------------------------------


def test_the_committed_seed_file_is_valid_json_and_starts_empty():
    payload = json.loads(Path(DEFAULT_SEED).read_text())
    assert payload["trips"] == [], "no invented trips should be committed"
    assert "_readme" in payload and "_example" in payload


def test_the_example_in_the_seed_file_actually_applies(db):
    payload = json.loads(Path(DEFAULT_SEED).read_text())
    report = apply_seed(db, {"trips": [payload["_example"]]}, create=True)
    assert report.problems == []
    assert only_match(db)["stadium"] == "Signal Iduna Park"
