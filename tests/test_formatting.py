"""Presentation helpers — mostly about not blowing Discord's field limits."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord

from bot.formatting import (
    DESCRIPTION_LIMIT,
    chunk_lines,
    fill,
    fmt_nok,
    fmt_score,
    kickoff_ts,
    local_day,
    medal,
    truncate,
)

OSLO = ZoneInfo("Europe/Oslo")


def test_fmt_nok_uses_thin_spaces_and_a_dash_for_unknown():
    assert fmt_nok(1349) == "1 349 kr"
    assert fmt_nok(99) == "99 kr"
    assert fmt_nok(None) == "—"


def test_fmt_score_needs_both_halves():
    assert fmt_score(2, 1) == "2–1"
    assert fmt_score(0, 0) == "0–0"
    assert fmt_score(None, 1) == "–"


def test_kickoff_ts_emits_a_discord_timestamp_tag():
    kickoff = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
    assert kickoff_ts(kickoff, "t") == "<t:1787338800:t>"


def test_local_day_renders_in_the_configured_timezone():
    # 21:30 UTC on a Friday is already Saturday in Oslo.
    late = datetime(2026, 8, 21, 22, 30, tzinfo=UTC)
    assert local_day(late, OSLO) == "Saturday 22 August"


def test_truncate_keeps_within_the_limit():
    assert truncate("short", 10) == "short"
    long = truncate("x" * 50, 10)
    assert len(long) == 10 and long.endswith("…")


def test_medal_for_the_podium_then_plain_numbers():
    assert medal(1) == "🥇"
    assert medal(3) == "🥉"
    assert medal(4) == "`4.`"


def test_chunk_lines_never_exceeds_the_limit_and_keeps_order():
    lines = [f"line {i} " + "y" * 90 for i in range(30)]
    blocks = chunk_lines(lines, limit=1000)
    assert all(len(b) <= 1000 for b in blocks)
    assert "\n".join(blocks).count("line ") == 30
    assert blocks[0].startswith("line 0")


def test_chunk_lines_of_nothing_is_nothing():
    assert chunk_lines([]) == []


def test_a_single_overlong_line_is_still_emitted():
    blocks = chunk_lines(["z" * 1500], limit=1000)
    assert len(blocks) == 1


# -- filling an embed body --------------------------------------------------


def test_fill_uses_the_description_and_adds_no_fields():
    # Every embed field renders its name on its own line, so a field named with
    # a zero-width space shows up as a blank row. The description has no name.
    e = fill(discord.Embed(title="t"), ["first", "second"])
    assert e.description == "first\nsecond"
    assert len(e.fields) == 0


def test_fill_keeps_an_existing_description_and_separates_it():
    e = fill(discord.Embed(title="t", description="intro"), ["a"])
    assert e.description == "intro\n\na"


def test_fill_of_nothing_leaves_an_empty_description():
    e = fill(discord.Embed(title="t"), [])
    assert not e.description
    assert len(e.fields) == 0


def test_fill_spills_into_fields_only_when_the_description_cannot_hold_it():
    lines = ["x" * 500] * 20  # 10k characters, far past the 4096 budget
    e = fill(discord.Embed(title="t"), lines)
    assert e.description is None
    assert len(e.fields) > 1, "a dropped list would be worse than a blank row"
    assert sum(len(f.value) for f in e.fields) >= 10_000 - 100


def test_fill_fits_a_realistic_archive_in_the_description():
    # 40 trips at two lines each — the MAX_LIST_ROWS ceiling.
    lines = ["**2014**  Germany, Dortmund", "  Dortmund 0–3 Bayern"] * 40
    e = fill(discord.Embed(title="t"), lines)
    assert e.description is not None and len(e.fields) == 0
    assert len(e.description) < DESCRIPTION_LIMIT
