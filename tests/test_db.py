"""Storage behaviour: the schema, the idempotency guards, and the mirror upsert."""

from __future__ import annotations

from bot.db import SCHEMA_VERSION, parse_utc, sql_str_tuple

MATCH = {
    "match_id": 500,
    "competition": "PL",
    "matchday": 1,
    "home_id": 57,
    "home": "Arsenal",
    "away_id": 61,
    "away": "Chelsea",
    "kickoff_utc": "2026-08-21T19:00:00+00:00",
    "status": "TIMED",
    "home_goals": None,
    "away_goals": None,
}


def test_schema_creates_every_table(db):
    names = {
        r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "wines",
        "wine_ratings",
        "members",
        "matches",
        "predictions",
        "prediction_scores",
        "reminders_sent",
        "announcements",
        "guild_channels",
        "bot_messages",
        "trips",
        "trip_matches",
        "trip_goals",
    } <= names
    assert "guild_config" not in names, "replaced by guild_channels"


def test_migrate_is_safe_to_run_again(db):
    db.migrate()
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION


def test_channel_config_round_trips_and_updates(db):
    db.set_channel(1, "wine", 111)
    db.set_channel(1, "football", 222)
    assert db.get_channel(1, "wine") == 111
    assert db.get_channel(1, "football") == 222
    db.set_channel(1, "wine", 333)
    assert db.get_channel(1, "wine") == 333
    assert db.get_channel(1, "football") == 222, "setting one kind must not clear another"


def test_configured_guilds_only_lists_guilds_with_that_channel(db):
    db.set_channel(1, "wine", 111)
    db.set_channel(2, "football", 222)
    assert db.configured_guilds("football") == [(2, 222)]


def test_favourite_teams_groups_members_by_club(db):
    db.set_favourite_team(10, 61, "Chelsea FC")
    db.set_favourite_team(11, 61, "Chelsea FC")
    db.set_favourite_team(12, 57, "Arsenal FC")
    assert db.favourite_teams() == {61: [10, 11], 57: [12]}


def test_clearing_a_favourite_team_removes_it_from_the_mapping(db):
    db.set_favourite_team(10, 61, "Chelsea FC")
    db.set_favourite_team(10, None, None)
    assert db.favourite_teams() == {}


def test_reminder_can_only_be_claimed_once(db):
    assert db.claim_reminder(1, "kickoff:5") is True
    assert db.claim_reminder(1, "kickoff:5") is False
    assert db.claim_reminder(1, "kickoff:6") is True, "a different guild claims separately"


def test_announcement_can_only_be_claimed_once(db):
    assert db.claim_announcement("mw:PL:1") is True
    assert db.claim_announcement("mw:PL:1") is False


def test_upsert_matches_updates_status_and_score_in_place(db):
    db.upsert_matches([MATCH])
    db.upsert_matches([{**MATCH, "status": "FINISHED", "home_goals": 2, "away_goals": 1}])
    rows = db.query("SELECT * FROM matches")
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["home_goals"], rows[0]["away_goals"]) == ("FINISHED", 2, 1)


def test_upsert_matches_handles_an_empty_batch(db):
    assert db.upsert_matches([]) == 0


def test_deleting_a_wine_cascades_to_its_ratings(db):
    wine_id = db.execute(
        "INSERT INTO wines (name, added_by, added_at) "
        "VALUES ('Barolo', 1, '2026-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO wine_ratings (wine_id, user_id, score, rated_at) "
        "VALUES (?, 1, 92, '2026-01-01T00:00:00+00:00')",
        (wine_id,),
    )
    db.execute("DELETE FROM wines WHERE id=?", (wine_id,))
    assert db.query("SELECT * FROM wine_ratings") == []


def test_parse_utc_accepts_both_z_and_offset_forms():
    assert parse_utc("2026-08-21T19:00:00Z") == parse_utc("2026-08-21T19:00:00+00:00")


def test_parse_utc_normalises_other_offsets_to_utc():
    assert parse_utc("2026-08-21T21:00:00+02:00").hour == 19


def test_kickoff_strings_sort_chronologically():
    # Mirror timestamps are compared as strings in SQL, so the format must sort.
    early = parse_utc("2026-08-21T19:00:00Z").isoformat(timespec="seconds")
    late = parse_utc("2026-08-22T13:30:00Z").isoformat(timespec="seconds")
    assert early < late


def test_sql_str_tuple_renders_valid_sql_for_any_length(db):
    assert sql_str_tuple(("A", "B")) == "('A', 'B')"
    assert sql_str_tuple(("A",)) == "('A')"
    # The point of the helper: it has to survive being interpolated into SQL.
    db.query(f"SELECT * FROM matches WHERE status IN {sql_str_tuple(('TIMED',))}")
