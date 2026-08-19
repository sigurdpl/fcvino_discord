"""Prediction scoring: 3 for the exact score, 1 for the right result, else 0."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.cogs.predictions import (
    POINTS_EXACT,
    POINTS_OUTCOME,
    Predictions,
    outcome,
    score_prediction,
)
from bot.db import utcnow_iso


@pytest.mark.parametrize(
    "home,away,expected",
    [(2, 1, 1), (0, 0, 0), (1, 3, -1)],
)
def test_outcome(home, away, expected):
    assert outcome(home, away) == expected


def test_exact_score_scores_three():
    assert score_prediction(2, 1, 2, 1) == POINTS_EXACT


def test_right_result_wrong_score_scores_one():
    assert score_prediction(3, 1, 2, 1) == POINTS_OUTCOME


def test_predicted_draw_with_wrong_scoreline_still_scores_one():
    assert score_prediction(1, 1, 2, 2) == POINTS_OUTCOME


def test_wrong_result_scores_nothing():
    assert score_prediction(2, 1, 1, 2) == 0
    assert score_prediction(1, 1, 2, 1) == 0


def test_zero_zero_predicted_exactly_scores_three():
    assert score_prediction(0, 0, 0, 0) == POINTS_EXACT


# -- the scoring pass over the database -------------------------------------


def _cog(db, competition="PL"):
    """A Predictions cog with only the collaborators _score_finished touches."""
    cog = Predictions.__new__(Predictions)
    cog.bot = SimpleNamespace(db=db, cfg=SimpleNamespace(prediction_competition=competition))
    return cog


def _match(db, match_id, *, status="FINISHED", home_goals=2, away_goals=1, matchday=1):
    db.upsert_matches(
        [
            {
                "match_id": match_id,
                "competition": "PL",
                "matchday": matchday,
                "home_id": 57,
                "home": "Arsenal",
                "away_id": 61,
                "away": "Chelsea",
                "kickoff_utc": "2026-08-21T19:00:00+00:00",
                "status": status,
                "home_goals": home_goals,
                "away_goals": away_goals,
            }
        ]
    )


def _predict(db, match_id, user_id, home, away):
    db.execute(
        """INSERT INTO predictions (match_id, user_id, home_goals, away_goals, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (match_id, user_id, home, away, utcnow_iso()),
    )


def test_score_finished_awards_points_per_prediction(db):
    _match(db, 1, home_goals=2, away_goals=1)
    _predict(db, 1, 100, 2, 1)  # exact
    _predict(db, 1, 200, 3, 1)  # right result
    _predict(db, 1, 300, 0, 2)  # wrong

    assert _cog(db)._score_finished() == 3
    points = {
        r["user_id"]: r["points"]
        for r in db.query("SELECT user_id, points FROM prediction_scores")
    }
    assert points == {100: POINTS_EXACT, 200: POINTS_OUTCOME, 300: 0}


def test_score_finished_is_idempotent(db):
    _match(db, 1)
    _predict(db, 1, 100, 2, 1)
    cog = _cog(db)
    assert cog._score_finished() == 1
    assert cog._score_finished() == 0, "a second pass must not re-score"
    assert db.query_one("SELECT COUNT(*) AS n FROM prediction_scores")["n"] == 1


def test_unfinished_matches_are_not_scored(db):
    _match(db, 1, status="TIMED", home_goals=None, away_goals=None)
    _predict(db, 1, 100, 2, 1)
    assert _cog(db)._score_finished() == 0


def test_postponed_match_with_no_score_is_not_scored(db):
    _match(db, 1, status="POSTPONED", home_goals=None, away_goals=None)
    _predict(db, 1, 100, 1, 0)
    assert _cog(db)._score_finished() == 0


def test_current_matchday_is_the_next_one_with_unplayed_games(db):
    _match(db, 1, status="FINISHED", matchday=1)
    _match(db, 2, status="TIMED", home_goals=None, away_goals=None, matchday=2)
    assert _cog(db)._current_matchday() == 2


def test_leaderboard_orders_by_points_then_exact_hits(db):
    _match(db, 1, home_goals=2, away_goals=1)
    _predict(db, 1, 100, 2, 1)
    _predict(db, 1, 200, 3, 1)
    _cog(db)._score_finished()

    e = _cog(db)._leaderboard_embed(None)
    assert e is not None
    assert e.description.index("<@100>") < e.description.index("<@200>")


def test_leaderboard_is_none_before_anything_is_scored(db):
    assert _cog(db)._leaderboard_embed(None) is None


# -- matchweek gating -------------------------------------------------------


def _timed(db, match_id, *, matchday, hours, status="TIMED", home=None, away=None):
    from datetime import timedelta

    from bot.db import utcnow

    db.upsert_matches(
        [
            {
                "match_id": match_id,
                "competition": "PL",
                "matchday": matchday,
                "home_id": 57,
                "home": "Arsenal",
                "away_id": 61,
                "away": "Chelsea",
                "kickoff_utc": (utcnow() + timedelta(hours=hours)).isoformat(timespec="seconds"),
                "status": status,
                "home_goals": home,
                "away_goals": away,
            }
        ]
    )


def test_matchday_in_view_picks_the_soonest_unplayed_matchweek(db):
    _timed(db, 1, matchday=2, hours=100)
    _timed(db, 2, matchday=1, hours=30)
    found = _cog(db)._matchday_in_view()
    assert found is not None and found[0] == 1


def test_matchday_in_view_ignores_a_matchweek_beyond_the_horizon(db):
    _timed(db, 1, matchday=5, hours=24 * 20)
    assert _cog(db)._matchday_in_view() is None


def test_matchday_in_view_ignores_matchweeks_already_played(db):
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    assert _cog(db)._matchday_in_view() is None


def test_a_matchweek_is_complete_only_when_nothing_is_left_to_play(db):
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    _timed(db, 2, matchday=1, hours=30, status="TIMED")
    _predict(db, 1, 100, 2, 1)
    assert _cog(db)._completed_matchdays() == []

    _timed(db, 2, matchday=1, hours=-2, status="FINISHED", home=0, away=0)
    assert _cog(db)._completed_matchdays() == [1]


def test_a_matchweek_nobody_played_gets_no_wrapup(db):
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    assert _cog(db)._completed_matchdays() == []


def test_a_postponed_fixture_does_not_hold_up_the_wrapup(db):
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    _timed(db, 2, matchday=1, hours=-28, status="POSTPONED")
    _predict(db, 1, 100, 2, 1)
    assert _cog(db)._completed_matchdays() == [1]


def test_multiple_predictions_do_not_inflate_the_completeness_check(db):
    # The query LEFT JOINs predictions, so each match is counted once per
    # prediction. The gate has to survive that.
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    _timed(db, 2, matchday=1, hours=30, status="TIMED")
    for user_id in range(100, 109):
        _predict(db, 1, user_id, 2, 1)
    assert _cog(db)._completed_matchdays() == [], "one unplayed fixture still blocks it"


def test_open_matches_exclude_kicked_off_and_finished_fixtures(db):
    _timed(db, 1, matchday=1, hours=5)
    _timed(db, 2, matchday=1, hours=-1)
    _timed(db, 3, matchday=1, hours=8, status="FINISHED", home=1, away=0)
    assert [r["match_id"] for r in _cog(db)._open_matches()] == [1]


def test_open_matches_only_covers_the_prediction_competition(db):
    _timed(db, 1, matchday=1, hours=5)
    db.upsert_matches(
        [
            {
                "match_id": 2,
                "competition": "CL",
                "matchday": 1,
                "home": "Real",
                "away": "Bayern",
                "kickoff_utc": "2099-01-01T19:00:00+00:00",
                "status": "TIMED",
            }
        ]
    )
    assert [r["match_id"] for r in _cog(db)._open_matches()] == [1]


def test_my_predictions_are_keyed_by_match(db):
    _timed(db, 1, matchday=1, hours=5)
    _timed(db, 2, matchday=1, hours=6)
    _predict(db, 1, 100, 2, 1)
    _predict(db, 2, 200, 0, 3)
    assert _cog(db)._my_predictions(100, [1, 2]) == {1: (2, 1)}


def test_my_predictions_of_no_matches_is_empty(db):
    assert _cog(db)._my_predictions(100, []) == {}


def test_prediction_counts_report_participation_without_revealing_picks(db):
    _timed(db, 1, matchday=1, hours=5)
    for user_id in (100, 200, 300):
        _predict(db, 1, user_id, 1, 1)
    assert _cog(db)._prediction_counts([1]) == {1: 3}


def test_matchday_leaderboard_only_counts_that_matchweek(db):
    _timed(db, 1, matchday=1, hours=-30, status="FINISHED", home=2, away=1)
    _timed(db, 2, matchday=2, hours=-2, status="FINISHED", home=0, away=0)
    _predict(db, 1, 100, 2, 1)  # 3 pts in MW1
    _predict(db, 2, 100, 0, 0)  # 3 pts in MW2
    _cog(db)._score_finished()

    season = _cog(db)._leaderboard_embed(None)
    week_one = _cog(db)._leaderboard_embed(1)
    assert "**6** pts" in season.description
    assert "**3** pts" in week_one.description
