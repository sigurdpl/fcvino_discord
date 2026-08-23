"""The pinned trips archive.

Same contract as the pinned league table: one message, edited in place, quiet
when nothing changed, and repaired if someone deletes it. Plus the thing the
table does not do — refreshing the moment a trip changes rather than waiting for
the daily pass.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot.cogs.trips import LIST_INDENT, Trips

GUILD_ID, CHANNEL_ID = 900, 901


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.response = FakeResponse()
        self.user = SimpleNamespace(id=user_id)
        self.guild = None
        self.channel = None


@pytest.fixture()
def channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = CHANNEL_ID
    ch.name = "trips"
    ch.send.return_value = message_mock()
    ch.fetch_message.return_value = message_mock()
    return ch


def message_mock(message_id=5000):
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    return msg


@pytest.fixture()
def cog(db, channel):
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.return_value = channel
    instance = Trips.__new__(Trips)
    instance.bot = SimpleNamespace(
        db=db, get_guild=lambda gid: guild if gid == GUILD_ID else None
    )
    return instance


def call(command, cog, **kwargs):
    interaction = FakeInteraction()
    asyncio.run(command.callback(cog, interaction, **kwargs))
    return interaction.response


def add_trip(cog, year=2014, country="Germany", city="Dortmund"):
    return call(Trips.add, cog, year=year, country=country, city=city)


def add_match(cog, year=2014, home="Dortmund", away="Bayern", hg=0, ag=3, **extra):
    return call(
        Trips.add_match, cog, year=year, home=home, away=away,
        home_goals=hg, away_goals=ag, **extra,
    )


def run_loop(cog):
    asyncio.run(Trips.list_loop.coro(cog))


def embed_of(call_args):
    return call_args.kwargs["embed"]


def text_of(e):
    return "\n".join(
        [e.title or "", e.description or "", *(f.value for f in e.fields), e.footer.text or ""]
    )


# -- the rendered view ------------------------------------------------------


def test_the_view_is_none_while_nothing_is_recorded(cog):
    assert cog._list_view() is None


def test_a_trip_renders_over_two_lines_with_its_matches_beneath(cog, db):
    add_trip(cog)
    add_match(cog)
    view = cog._list_view()
    assert view.count == 1
    assert view.lines[0] == f"**2014**{LIST_INDENT}Germany, Dortmund"
    assert view.lines[1] == f"{LIST_INDENT}Dortmund **0–3** Bayern"


def test_the_indent_survives_discord_collapsing_spaces(cog, db):
    # Ordinary leading spaces are collapsed in message text; em spaces are not.
    add_trip(cog)
    add_match(cog)
    assert cog._list_view().lines[1].startswith(" ")


def test_a_trip_with_no_match_says_so(cog, db):
    add_trip(cog)
    assert "_no match recorded_" in cog._list_view().lines[1]


def test_the_summary_counts_countries_matches_goals_and_grounds(cog, db):
    add_trip(cog, 2014, "Germany", "Dortmund")
    add_match(cog, 2014, stadium="Signal Iduna Park")
    add_trip(cog, 2016, "Italy", "Rome")
    add_match(cog, 2016, home="Roma", away="Lazio", hg=4, ag=3, stadium="Olimpico")
    summary = cog._list_view().summary
    assert "**2** countries" in summary
    assert "**2** matches" in summary
    assert "**10** goals" in summary
    assert "**2** grounds" in summary


def test_the_fingerprint_ignores_the_footer_date(cog, db):
    # Otherwise the daily pass would re-edit an unchanged message every day.
    add_trip(cog)
    view = cog._list_view()
    first = cog._list_embed(view, changed_on=date(2026, 1, 1))
    second = cog._list_embed(view, changed_on=date(2026, 8, 23))
    assert first.footer.text != second.footer.text
    assert cog._list_view().fingerprint == view.fingerprint


def test_the_fingerprint_moves_when_a_trip_changes(cog, db):
    add_trip(cog)
    before = cog._list_view().fingerprint
    add_match(cog)
    assert cog._list_view().fingerprint != before


def test_the_list_uses_no_fields_so_there_are_no_blank_rows(cog, db):
    # A field named with a zero-width space renders as an empty line, which is
    # what put a blank row between each year and its matches.
    add_trip(cog)
    add_match(cog)
    e = call(Trips.list_trips, cog).messages[-1][1]["embed"]
    assert len(e.fields) == 0
    lines = e.description.split("\n")
    assert lines[0].startswith("**2014**")
    assert lines[1].strip().startswith("Dortmund"), "the fixture follows the year directly"
    assert "" not in lines[:2]


def test_the_command_and_the_pinned_copy_render_the_same_lines(cog, db):
    add_trip(cog)
    add_match(cog)
    response = call(Trips.list_trips, cog)
    from_command = response.messages[-1][1]["embed"]
    from_pinned = cog._list_embed(cog._list_view(), changed_on=date.today())
    assert [f.value for f in from_command.fields] == [f.value for f in from_pinned.fields]
    assert from_command.footer.text is None, "only the pinned copy dates itself"


# -- the pinned message -----------------------------------------------------


def test_nothing_is_posted_until_a_channel_is_configured(cog, db, channel):
    add_trip(cog)
    run_loop(cog)
    assert channel.send.call_count == 0


def test_deleting_the_last_trip_corrects_the_pinned_message(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    add_trip(cog)
    existing = channel.fetch_message.return_value

    call(Trips.remove, cog, year=2014)
    assert existing.edit.await_count == 1, "a stale list of deleted trips is a lie"
    assert "No trips recorded" in embed_of(existing.edit.call_args).title


def test_an_empty_archive_with_no_pinned_message_stays_silent(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    run_loop(cog)
    assert channel.send.call_count == 0


def test_the_first_pass_posts_and_pins_the_archive(cog, db, channel):
    # Trips recorded before the channel was configured still get published.
    add_trip(cog)
    add_match(cog)
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)

    run_loop(cog)
    assert channel.send.call_count == 1
    body = text_of(embed_of(channel.send.call_args))
    assert "1 trips" in body and "Dortmund" in body
    channel.send.return_value.pin.assert_awaited_once()
    assert db.get_bot_message(GUILD_ID, "trips")["message_id"] == 5000


def test_an_unchanged_archive_is_left_completely_alone(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()
    add_trip(cog)
    channel.send.reset_mock()

    run_loop(cog)
    assert channel.send.call_count == 0
    assert channel.fetch_message.call_count == 0, "not even fetched — the hash matched"


def test_a_deleted_message_is_reposted(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()
    add_trip(cog)

    channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    channel.send.return_value = message_mock(6000)

    add_match(cog)
    assert db.get_bot_message(GUILD_ID, "trips")["message_id"] == 6000


def test_moving_the_channel_posts_a_fresh_message_there(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()
    add_trip(cog)

    other = MagicMock(spec=discord.TextChannel)
    other.id = 999
    other.name = "trips-2"
    other.send.return_value = message_mock(7000)
    db.set_channel(GUILD_ID, "trips", 999)
    cog.bot.get_guild(GUILD_ID).get_channel.return_value = other

    run_loop(cog)
    stored = db.get_bot_message(GUILD_ID, "trips")
    assert (stored["channel_id"], stored["message_id"]) == (999, 7000)


def test_a_failed_pin_does_not_lose_the_message(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    posted = message_mock()
    posted.pin.side_effect = discord.Forbidden(MagicMock(status=403), "no Manage Messages")
    channel.send.return_value = posted
    add_trip(cog)
    assert db.get_bot_message(GUILD_ID, "trips")["message_id"] == 5000


# -- refreshing the moment something changes --------------------------------


def test_adding_a_trip_updates_the_pinned_copy_immediately(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()

    response = add_trip(cog)
    assert "Recorded" in (response.messages[-1][0] or "")
    assert channel.send.call_count == 1, "no waiting for the daily pass"


@pytest.mark.parametrize("action", ["add_match", "remove_match", "remove"])
def test_every_mutating_command_refreshes_the_pinned_copy(cog, db, channel, action):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()
    add_trip(cog)
    add_match(cog)
    existing = channel.fetch_message.return_value
    existing.edit.reset_mock()

    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    if action == "add_match":
        add_match(cog, home="Schalke", away="Koeln", hg=1, ag=1)
    elif action == "remove_match":
        call(Trips.remove_match, cog, match=str(match_id))
    else:
        call(Trips.remove, cog, year=2014)

    assert existing.edit.await_count == 1, f"{action} should have updated the pinned copy"


def test_recording_a_scorer_does_not_disturb_the_pinned_list(cog, db, channel):
    # The list shows fixtures and scores, not scorers, so its content is
    # genuinely unchanged — the fingerprint is doing real work here rather than
    # the refresh simply not being wired up.
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    add_trip(cog)
    add_match(cog)
    existing = channel.fetch_message.return_value
    existing.edit.reset_mock()

    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    call(Trips.add_goals, cog, match=str(match_id), goals="23 Robben A")
    assert existing.edit.await_count == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_goals")["n"] == 1, "still recorded"


def test_a_change_that_leaves_the_text_identical_does_not_re_edit(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.return_value = message_mock()
    add_trip(cog)
    existing = channel.fetch_message.return_value

    add_trip(cog)  # same year, same country, same city
    assert existing.edit.await_count == 0


def test_a_broken_channel_does_not_break_the_command(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no Send Messages")

    response = add_trip(cog)
    assert "Recorded" in (response.messages[-1][0] or ""), "the trip was still saved"
    assert db.query_one("SELECT COUNT(*) AS n FROM trips")["n"] == 1
    assert db.get_bot_message(GUILD_ID, "trips") is None


# -- edits reach the pinned copy --------------------------------------------


def test_correcting_a_score_updates_the_pinned_copy(cog, db, channel):
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    add_trip(cog)
    add_match(cog, hg=1, ag=0)
    existing = channel.fetch_message.return_value
    existing.edit.reset_mock()

    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    call(Trips.edit_match, cog, match=str(match_id), home_goals=0, away_goals=3)

    assert existing.edit.await_count == 1
    assert "0–3" in text_of(embed_of(existing.edit.call_args))


def test_editing_something_the_list_does_not_show_leaves_it_alone(cog, db, channel):
    # The list shows fixtures and scores, not crowds — so the rendered text is
    # unchanged and the fingerprint correctly declines to re-edit.
    db.set_channel(GUILD_ID, "trips", CHANNEL_ID)
    add_trip(cog)
    add_match(cog)
    existing = channel.fetch_message.return_value
    existing.edit.reset_mock()

    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    call(Trips.edit_match, cog, match=str(match_id), attendance=80667)

    assert existing.edit.await_count == 0
    assert db.query_one("SELECT attendance FROM trip_matches")["attendance"] == 80667
