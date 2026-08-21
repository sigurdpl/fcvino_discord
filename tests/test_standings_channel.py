"""A channel that shows the league table.

A Discord channel cannot invoke a slash command, so this is the bot posting one
message and editing it from then on. The behaviour worth pinning down: it edits
rather than reposting, it stays quiet when the table hasn't moved, and it
recovers if someone deletes the message.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from bot.cogs.football import Football
from bot.db import Database

GUILD_ID, CHANNEL_ID = 900, 901


def standings_payload(*, leader="Arsenal", points=12, played=5):
    return {
        "standings": [
            {
                "type": "TOTAL",
                "group": None,
                "table": [
                    {
                        "position": 1,
                        "team": {"shortName": leader, "name": f"{leader} FC"},
                        "playedGames": played,
                        "goalDifference": 7,
                        "points": points,
                    },
                    {
                        "position": 2,
                        "team": {"shortName": "Chelsea", "name": "Chelsea FC"},
                        "playedGames": played,
                        "goalDifference": 3,
                        "points": 10,
                    },
                ],
            },
            {"type": "HOME", "table": [{"position": 1, "team": {"shortName": "Ignored"}}]},
        ]
    }


class StubAPI:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0

    async def standings(self, code):
        self.calls += 1
        return self.payload


@pytest.fixture()
def channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = CHANNEL_ID
    ch.name = "premier-league-table"
    return ch


def message_mock(message_id=5000):
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    return msg


def cog(db, api, channel):
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.return_value = channel
    instance = Football.__new__(Football)
    instance._tick = 0
    instance.bot = SimpleNamespace(
        db=db,
        football=api,
        get_guild=lambda gid: guild if gid == GUILD_ID else None,
        cfg=SimpleNamespace(
            reminder_competitions=("PL",),
            prediction_competition="PL",
            reminder_lead_minutes=60,
            tz=ZoneInfo("Europe/Oslo"),
        ),
    )
    return instance


def run(instance):
    asyncio.run(Football.standings_loop.coro(instance))


# -- rendering and fingerprinting -------------------------------------------


def test_the_embed_uses_the_overall_table_only(db, channel):
    instance = cog(db, StubAPI(standings_payload()), channel)
    e = instance._standings_embed(standings_payload(), "Premier League")
    assert e is not None
    body = e.fields[0].value
    assert "Arsenal" in body and "Chelsea" in body
    assert "Ignored" not in body, "the HOME table must not be rendered"


def test_no_published_table_renders_nothing(db, channel):
    instance = cog(db, StubAPI({}), channel)
    assert instance._standings_embed({"standings": []}, "Premier League") is None


def test_an_empty_table_renders_nothing(db, channel):
    instance = cog(db, StubAPI({}), channel)
    payload = {"standings": [{"type": "TOTAL", "table": []}]}
    assert instance._standings_embed(payload, "Premier League") is None


def test_the_fingerprint_tracks_points_and_position(db, channel):
    instance = cog(db, StubAPI({}), channel)
    base = instance._standings_hash(standings_payload())
    assert base == instance._standings_hash(standings_payload()), "must be stable"
    assert base != instance._standings_hash(standings_payload(points=13))
    assert base != instance._standings_hash(standings_payload(leader="Liverpool"))
    assert base != instance._standings_hash(standings_payload(played=6))


# -- the loop ---------------------------------------------------------------


def test_nothing_happens_until_a_standings_channel_is_set(db, channel):
    api = StubAPI(standings_payload())
    run(cog(db, api, channel))
    assert api.calls == 0, "no channel means no API call at all"
    assert channel.send.call_count == 0


def test_the_first_pass_posts_and_pins_the_table(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    channel.send.return_value = message_mock()
    run(cog(db, StubAPI(standings_payload()), channel))

    assert channel.send.call_count == 1
    assert channel.send.call_args.kwargs["embed"].title.endswith("Premier League")
    channel.send.return_value.pin.assert_awaited_once()
    stored = db.get_bot_message(GUILD_ID, "standings")
    assert stored["message_id"] == 5000
    assert stored["channel_id"] == CHANNEL_ID


def test_an_unchanged_table_is_left_completely_alone(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    channel.send.return_value = message_mock()
    instance = cog(db, StubAPI(standings_payload()), channel)
    run(instance)
    channel.send.reset_mock()

    run(instance)
    assert channel.send.call_count == 0
    assert channel.fetch_message.call_count == 0, "not even fetched — the hash matched"


def test_a_changed_table_edits_the_same_message(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    channel.send.return_value = message_mock()
    api = StubAPI(standings_payload())
    instance = cog(db, api, channel)
    run(instance)

    existing = message_mock()
    channel.fetch_message.return_value = existing
    api.payload = standings_payload(points=15)
    channel.send.reset_mock()
    run(instance)

    existing.edit.assert_awaited_once()
    assert channel.send.call_count == 0, "editing, not reposting"
    assert db.get_bot_message(GUILD_ID, "standings")["message_id"] == 5000


def test_a_deleted_message_is_reposted(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    channel.send.return_value = message_mock()
    api = StubAPI(standings_payload())
    instance = cog(db, api, channel)
    run(instance)

    channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    channel.send.return_value = message_mock(6000)
    api.payload = standings_payload(points=15)
    run(instance)

    assert db.get_bot_message(GUILD_ID, "standings")["message_id"] == 6000


def test_moving_the_channel_posts_a_fresh_message_there(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    channel.send.return_value = message_mock()
    instance = cog(db, StubAPI(standings_payload()), channel)
    run(instance)

    other = MagicMock(spec=discord.TextChannel)
    other.id = 999
    other.name = "new-table"
    other.send.return_value = message_mock(7000)
    db.set_channel(GUILD_ID, "standings", 999)
    instance.bot.get_guild(GUILD_ID).get_channel.return_value = other
    run(instance)

    assert other.send.call_count == 1, "same hash, but a different channel needs a message"
    stored = db.get_bot_message(GUILD_ID, "standings")
    assert (stored["channel_id"], stored["message_id"]) == (999, 7000)


def test_a_failed_pin_does_not_lose_the_message(db, channel):
    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)
    posted = message_mock()
    posted.pin.side_effect = discord.Forbidden(MagicMock(status=403), "no Manage Messages")
    channel.send.return_value = posted
    run(cog(db, StubAPI(standings_payload()), channel))
    assert db.get_bot_message(GUILD_ID, "standings")["message_id"] == 5000


def test_an_api_failure_leaves_the_existing_message_untouched(db, channel):
    from bot.football_api import FootballAPIError

    db.set_channel(GUILD_ID, "standings", CHANNEL_ID)

    class BrokenAPI(StubAPI):
        async def standings(self, code):
            raise FootballAPIError("upstream is down")

    run(cog(db, BrokenAPI(None), channel))
    assert channel.send.call_count == 0
    assert db.get_bot_message(GUILD_ID, "standings") is None


# -- channel storage --------------------------------------------------------


def test_channel_kinds_are_independent(db):
    db.set_channel(GUILD_ID, "standings", 1)
    db.set_channel(GUILD_ID, "football", 2)
    assert db.get_channel(GUILD_ID, "standings") == 1
    assert db.get_channel(GUILD_ID, "football") == 2
    db.set_channel(GUILD_ID, "standings", 3)
    assert db.get_channel(GUILD_ID, "football") == 2


def test_an_unknown_channel_kind_is_rejected(db):
    with pytest.raises(ValueError, match="unknown channel kind"):
        db.set_channel(GUILD_ID, "hockey", 1)
    with pytest.raises(ValueError, match="unknown channel kind"):
        db.get_channel(GUILD_ID, "hockey")


# -- migrating off the old column-per-kind table -----------------------------


def test_an_old_guild_config_is_carried_over_and_dropped(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE guild_config (
               guild_id               INTEGER PRIMARY KEY,
               football_channel_id    INTEGER,
               wine_channel_id        INTEGER,
               predictions_channel_id INTEGER
           );
           INSERT INTO guild_config VALUES (900, 11, 22, 33);
           INSERT INTO guild_config VALUES (901, 44, NULL, NULL);"""
    )
    old.commit()
    old.close()

    db = Database(path)
    db.connect()
    try:
        assert db.get_channel(900, "football") == 11
        assert db.get_channel(900, "wine") == 22
        assert db.get_channel(900, "predictions") == 33
        assert db.get_channel(901, "football") == 44
        assert db.get_channel(901, "wine") is None
        assert db.get_channel(900, "standings") is None, "the new kind starts unset"
        names = {
            r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "guild_config" not in names

        # And running it again is harmless.
        db.migrate()
        assert db.get_channel(900, "football") == 11
    finally:
        db.close()


def test_a_fresh_database_never_creates_the_old_table(db):
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "guild_config" not in names
    assert {"guild_channels", "bot_messages"} <= names
