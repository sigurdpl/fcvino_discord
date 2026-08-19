"""End-to-end passes over the background loops, with the API and Discord stubbed.

This is the closest thing to running the bot: mirror refresh, reminder posting,
scoring, and the matchweek wrap-up, all driven through the real loop bodies.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from bot.cogs.football import Football
from bot.cogs.predictions import Predictions
from bot.db import utcnow, utcnow_iso

ARSENAL, CHELSEA = 57, 61
GUILD_ID, CHANNEL_ID = 900, 901


def match(match_id, *, competition="PL", hours, status="TIMED", home=None, away=None, matchday=1):
    return {
        "match_id": match_id,
        "competition": competition,
        "matchday": matchday,
        "home_id": ARSENAL,
        "home": "Arsenal",
        "away_id": CHELSEA,
        "away": "Chelsea",
        "kickoff_utc": (utcnow() + timedelta(hours=hours)).isoformat(timespec="seconds"),
        "status": status,
        "home_goals": home,
        "away_goals": away,
    }


class StubAPI:
    """Stands in for FootballAPI, serving a fixed list of normalised matches."""

    def __init__(self, matches: list[dict]) -> None:
        self.matches = matches
        self.competition_calls: list[str] = []
        self.team_calls: list[int] = []

    async def competition_matches(self, code, *, date_from, date_to, status=None, ttl=None):
        self.competition_calls.append(code)
        return [m for m in self.matches if m["competition"] == code]

    async def team_matches(self, team_id, *, date_from, date_to, ttl=None):
        self.team_calls.append(team_id)
        return [m for m in self.matches if team_id in (m["home_id"], m["away_id"])]


@pytest.fixture()
def channel():
    # spec= keeps isinstance(channel, discord.TextChannel) true and makes send awaitable.
    return MagicMock(spec=discord.TextChannel)


def make_bot(db, api, channel):
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.return_value = channel
    return SimpleNamespace(
        db=db,
        football=api,
        get_guild=lambda guild_id: guild if guild_id == GUILD_ID else None,
        cfg=SimpleNamespace(
            reminder_competitions=("PL", "CL"),
            prediction_competition="PL",
            reminder_lead_minutes=60,
            tz=ZoneInfo("Europe/Oslo"),
        ),
    )


def football_cog(db, api, channel):
    cog = Football.__new__(Football)
    cog._tick = 0
    cog.bot = make_bot(db, api, channel)
    return cog


def predictions_cog(db, api, channel):
    cog = Predictions.__new__(Predictions)
    cog.bot = make_bot(db, api, channel)
    return cog


def run_football_loop(cog):
    asyncio.run(Football.reminder_loop.coro(cog))


def run_predictions_loop(cog):
    asyncio.run(Predictions.score_loop.coro(cog))


# -- reminders --------------------------------------------------------------


def test_a_kickoff_inside_the_lead_time_is_announced_once(db, channel):
    db.set_channel(GUILD_ID, "football", CHANNEL_ID)
    db.set_favourite_team(10, ARSENAL, "Arsenal FC")
    cog = football_cog(db, StubAPI([match(1, hours=0.5)]), channel)

    run_football_loop(cog)
    assert channel.send.call_count == 1
    assert channel.send.call_args.kwargs["content"] == "<@10>"

    run_football_loop(cog)
    assert channel.send.call_count == 1, "a second pass must not re-ping"


def test_a_kickoff_beyond_the_lead_time_waits(db, channel):
    db.set_channel(GUILD_ID, "football", CHANNEL_ID)
    cog = football_cog(db, StubAPI([match(1, hours=5)]), channel)
    run_football_loop(cog)
    assert channel.send.call_count == 0


def test_nothing_is_posted_until_a_channel_is_configured(db, channel):
    cog = football_cog(db, StubAPI([match(1, hours=0.5)]), channel)
    run_football_loop(cog)
    assert channel.send.call_count == 0
    # And the mirror still filled up, so /football fixtures works immediately.
    assert db.query_one("SELECT COUNT(*) AS n FROM matches")["n"] == 1


def test_favourite_clubs_are_refreshed_on_the_slower_cadence(db, channel):
    db.set_favourite_team(10, ARSENAL, "Arsenal FC")
    api = StubAPI([match(1, hours=0.5)])
    cog = football_cog(db, api, channel)
    for _ in range(4):
        run_football_loop(cog)
    assert api.team_calls == [ARSENAL], "once per four ticks, not every tick"
    assert api.competition_calls.count("PL") == 4


def test_an_api_failure_does_not_kill_the_loop(db, channel):
    from bot.football_api import FootballAPIError

    db.set_channel(GUILD_ID, "football", CHANNEL_ID)
    db.upsert_matches([match(1, hours=0.5)])

    class BrokenAPI(StubAPI):
        async def competition_matches(self, *args, **kwargs):
            raise FootballAPIError("upstream is down")

    cog = football_cog(db, BrokenAPI([]), channel)
    run_football_loop(cog)
    # The refresh failed, but the already-mirrored match was still announced.
    assert channel.send.call_count == 1


# -- the prediction game, start to finish -----------------------------------


def test_matchweek_is_announced_scored_and_wrapped_up(db, channel):
    db.set_channel(GUILD_ID, "predictions", CHANNEL_ID)
    fixtures = [match(1, hours=48), match(2, hours=50)]
    api = StubAPI(fixtures)
    cog = predictions_cog(db, api, channel)

    # 1. The matchweek comes into view and is announced exactly once.
    run_predictions_loop(cog)
    assert channel.send.call_count == 1
    assert "Matchweek 1 is open" in channel.send.call_args.kwargs["embed"].title
    run_predictions_loop(cog)
    assert channel.send.call_count == 1

    # 2. Two members predict.
    for user_id, (home, away) in {100: (2, 1), 200: (1, 1)}.items():
        for match_id in (1, 2):
            db.execute(
                """INSERT INTO predictions (match_id, user_id, home_goals, away_goals, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (match_id, user_id, home, away, utcnow_iso()),
            )

    # 3. The games are played.
    api.matches = [
        match(1, hours=-4, status="FINISHED", home=2, away=1),
        match(2, hours=-2, status="FINISHED", home=3, away=0),
    ]
    channel.send.reset_mock()
    run_predictions_loop(cog)

    points = {
        (r["user_id"], r["match_id"]): r["points"]
        for r in db.query("SELECT user_id, match_id, points FROM prediction_scores")
    }
    assert points == {
        (100, 1): 3,  # exact 2-1
        (100, 2): 1,  # right result, wrong score
        (200, 1): 0,  # predicted a draw
        (200, 2): 0,
    }

    # 4. The wrap-up and the table land, once.
    assert channel.send.call_count == 2
    titles = [call.kwargs["embed"].title for call in channel.send.call_args_list]
    assert "Matchweek 1 — how it went" in titles[0]
    assert "season table" in titles[1]

    channel.send.reset_mock()
    run_predictions_loop(cog)
    assert channel.send.call_count == 0, "nothing left to say"


def test_a_matchweek_nobody_predicted_is_not_wrapped_up(db, channel):
    db.set_channel(GUILD_ID, "predictions", CHANNEL_ID)
    api = StubAPI([match(1, hours=-4, status="FINISHED", home=2, away=1)])
    cog = predictions_cog(db, api, channel)
    run_predictions_loop(cog)
    assert channel.send.call_count == 0


def test_scoring_happens_even_with_no_channel_configured(db, channel):
    api = StubAPI([match(1, hours=-4, status="FINISHED", home=2, away=1)])
    cog = predictions_cog(db, api, channel)
    db.upsert_matches(api.matches)
    db.execute(
        """INSERT INTO predictions (match_id, user_id, home_goals, away_goals, created_at)
           VALUES (1, 100, 2, 1, ?)""",
        (utcnow_iso(),),
    )
    run_predictions_loop(cog)
    assert db.query_one("SELECT points FROM prediction_scores")["points"] == 3
    assert channel.send.call_count == 0
