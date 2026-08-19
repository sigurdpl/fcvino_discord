"""Mirror queries and reminder targeting.

The fixture/result queries interpolate a team clause and bind parameters in a
particular order, which is exactly the sort of thing that breaks silently.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.cogs.football import Football
from bot.db import utcnow

ARSENAL, CHELSEA, BODO = 57, 61, 1234


@pytest.fixture()
def cog(db):
    instance = Football.__new__(Football)
    instance._tick = 0
    instance.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(
            reminder_competitions=("PL", "CL"),
            prediction_competition="PL",
            reminder_lead_minutes=60,
            tz=ZoneInfo("Europe/Oslo"),
        ),
    )
    return instance


def add_match(
    db,
    match_id,
    *,
    competition="PL",
    hours_from_now=2.0,
    status="TIMED",
    home_id=ARSENAL,
    away_id=CHELSEA,
    home_goals=None,
    away_goals=None,
):
    kickoff = utcnow() + timedelta(hours=hours_from_now)
    db.upsert_matches(
        [
            {
                "match_id": match_id,
                "competition": competition,
                "matchday": 1,
                "home_id": home_id,
                "home": f"Team {home_id}",
                "away_id": away_id,
                "away": f"Team {away_id}",
                "kickoff_utc": kickoff.isoformat(timespec="seconds"),
                "status": status,
                "home_goals": home_goals,
                "away_goals": away_goals,
            }
        ]
    )
    return match_id


def test_upcoming_only_returns_unplayed_matches_in_the_window(cog, db):
    add_match(db, 1, hours_from_now=2)
    add_match(db, 2, hours_from_now=24 * 30)  # beyond the window
    add_match(db, 3, hours_from_now=-2)  # already kicked off
    add_match(db, 4, hours_from_now=3, status="FINISHED", home_goals=1, away_goals=0)
    rows = cog._upcoming(["PL"], days=7, team_ids=[])
    assert [r["match_id"] for r in rows] == [1]


def test_upcoming_ignores_competitions_we_do_not_ask_for(cog, db):
    add_match(db, 1, competition="PD")
    assert cog._upcoming(["PL", "CL"], days=7, team_ids=[]) == []


def test_upcoming_includes_a_favourite_club_outside_those_competitions(cog, db):
    add_match(db, 1, competition="ELC", home_id=BODO, away_id=CHELSEA)
    assert cog._upcoming(["PL", "CL"], days=7, team_ids=[]) == []
    rows = cog._upcoming(["PL", "CL"], days=7, team_ids=[BODO])
    assert [r["match_id"] for r in rows] == [1]


def test_upcoming_matches_a_favourite_club_playing_away(cog, db):
    add_match(db, 1, competition="ELC", home_id=CHELSEA, away_id=BODO)
    rows = cog._upcoming(["PL"], days=7, team_ids=[BODO])
    assert [r["match_id"] for r in rows] == [1]


def test_upcoming_does_not_duplicate_a_favourite_club_in_a_followed_league(cog, db):
    add_match(db, 1, competition="PL", home_id=ARSENAL)
    rows = cog._upcoming(["PL"], days=7, team_ids=[ARSENAL])
    assert [r["match_id"] for r in rows] == [1]


def test_upcoming_is_ordered_by_kickoff(cog, db):
    add_match(db, 1, hours_from_now=48)
    add_match(db, 2, hours_from_now=6)
    assert [r["match_id"] for r in cog._upcoming(["PL"], days=7, team_ids=[])] == [2, 1]


def test_recent_returns_finished_matches_newest_first(cog, db):
    add_match(db, 1, hours_from_now=-48, status="FINISHED", home_goals=2, away_goals=1)
    add_match(db, 2, hours_from_now=-3, status="FINISHED", home_goals=0, away_goals=0)
    add_match(db, 3, hours_from_now=-4, status="POSTPONED")
    rows = cog._recent(["PL"], days=3, team_ids=[])
    assert [r["match_id"] for r in rows] == [2, 1]


def test_recent_respects_the_day_window(cog, db):
    add_match(db, 1, hours_from_now=-24 * 10, status="FINISHED", home_goals=1, away_goals=1)
    assert cog._recent(["PL"], days=3, team_ids=[]) == []


def test_queries_survive_an_empty_competition_list(cog, db):
    add_match(db, 1)
    assert cog._upcoming([], days=7, team_ids=[]) == []
    assert cog._upcoming([], days=7, team_ids=[ARSENAL])[0]["match_id"] == 1


# -- reminder targeting -----------------------------------------------------


def test_a_followed_competition_is_always_relevant(cog, db):
    add_match(db, 1, competition="CL")
    row = db.query_one("SELECT * FROM matches WHERE match_id=1")
    assert cog._is_relevant(row, {}) is True


def test_an_unfollowed_competition_is_only_relevant_via_a_favourite_club(cog, db):
    add_match(db, 1, competition="DED", home_id=BODO)
    row = db.query_one("SELECT * FROM matches WHERE match_id=1")
    assert cog._is_relevant(row, {}) is False
    assert cog._is_relevant(row, {BODO: [999]}) is True


def test_reminder_tags_everyone_following_either_side_exactly_once(cog, db):
    add_match(db, 1, home_id=ARSENAL, away_id=CHELSEA)
    row = db.query_one("SELECT * FROM matches WHERE match_id=1")
    payload = cog._reminder_payload(row, {ARSENAL: [10, 11], CHELSEA: [11, 12]})
    assert payload["content"] == "<@10> <@11> <@12>"


def test_reminder_without_followers_has_no_mentions(cog, db):
    add_match(db, 1)
    row = db.query_one("SELECT * FROM matches WHERE match_id=1")
    payload = cog._reminder_payload(row, {})
    assert payload["content"] is None
    assert "Kickoff" in payload["embed"].description


def test_reminder_names_the_competition_in_full(cog, db):
    add_match(db, 1, competition="CL")
    row = db.query_one("SELECT * FROM matches WHERE match_id=1")
    payload = cog._reminder_payload(row, {})
    assert "UEFA Champions League" in payload["embed"].description


def test_team_codes_always_offer_the_premier_league_and_champions_league(cog):
    cog.bot.cfg.reminder_competitions = ("SA",)
    assert cog._team_codes() == ["SA", "PL", "CL"]


def test_team_codes_do_not_repeat(cog):
    cog.bot.cfg.reminder_competitions = ("PL", "CL")
    assert cog._team_codes() == ["PL", "CL"]
