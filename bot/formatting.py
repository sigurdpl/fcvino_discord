"""Shared presentation helpers: colours, embeds, and human-readable values."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

# Burgundy for wine, pitch green for football, a warm gold for leaderboards.
WINE_COLOUR = discord.Colour(0x722F37)
FOOTBALL_COLOUR = discord.Colour(0x1F8B4C)
TABLE_COLOUR = discord.Colour(0xD4AF37)
NEUTRAL_COLOUR = discord.Colour(0x5865F2)

MEDALS = ("🥇", "🥈", "🥉")


def embed(title: str, *, colour: discord.Colour, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def kickoff_ts(kickoff: datetime, style: str = "f") -> str:
    """A Discord timestamp tag, so every reader sees kickoff in their own timezone."""
    return discord.utils.format_dt(kickoff, style)  # type: ignore[arg-type]


def kickoff_relative(kickoff: datetime) -> str:
    return discord.utils.format_dt(kickoff, "R")  # type: ignore[arg-type]


def local_day(kickoff: datetime, tz: ZoneInfo) -> str:
    """Day header used to group fixtures, e.g. 'Friday 21 August'."""
    return kickoff.astimezone(tz).strftime("%A %-d %B")


def fmt_nok(price: int | None) -> str:
    if price is None:
        return "—"
    return f"{price:,} kr".replace(",", " ")


def fmt_score(home: int | None, away: int | None) -> str:
    if home is None or away is None:
        return "–"
    return f"{home}–{away}"


def wine_label(row, *, with_vintage: bool = True) -> str:
    """'Barolo 2018 — Vietti' from a wines row, skipping missing pieces.

    The vintage is only appended when the name doesn't already state it. Most of
    the imported names end in their vintage, and it was read *out of* the name in
    the first place, so appending unconditionally gives "Côte-Rôtie 2007 2007".
    """
    parts = [row["name"]]
    vintage = str(row["vintage"]) if row["vintage"] else None
    if with_vintage and vintage and vintage not in row["name"]:
        parts.append(vintage)
    label = " ".join(parts)
    if row["producer"]:
        label = f"{label} — {row['producer']}"
    return label


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def medal(position: int) -> str:
    """1-indexed position -> medal or plain number."""
    if 1 <= position <= 3:
        return MEDALS[position - 1]
    return f"`{position}.`"


DESCRIPTION_LIMIT = 4096


def fill(e: discord.Embed, lines: list[str]) -> discord.Embed:
    """Put `lines` into the embed's description.

    Not into fields: Discord renders every field's *name* on its own line, so a
    field named with a zero-width space shows up as a blank row above its
    content. The description has no name, and its 4096-character budget is four
    times a field's, so long lists stop needing to be split at all.

    Anything already in the description is kept, with a blank line between.
    Content too large even for the description falls back to fields, blank rows
    and all — better a stray blank line than a dropped list.
    """
    body = "\n".join(lines)
    existing = e.description or ""
    if len(existing) + len(body) + 2 <= DESCRIPTION_LIMIT:
        e.description = f"{existing}\n\n{body}" if existing else body
        return e
    for block in chunk_lines(lines):
        e.add_field(name="\u200b", value=block, inline=False)
    return e


def chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    """Group lines into blocks that fit an embed field, preserving order."""
    blocks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if current and length + len(line) + 1 > limit:
            blocks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        blocks.append("\n".join(current))
    return blocks
