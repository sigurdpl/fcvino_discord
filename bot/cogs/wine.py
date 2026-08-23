"""The cellar: log bottles, rate them out of 100, keep the notes.

Ratings are 1–100 because that is how wine actually gets talked about. A bottle
needs MIN_RATINGS_FOR_BOARD scores before it can appear on a board, so one
enthusiast can't crown their own pick.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from ..db import utcnow_iso
from ..formatting import (
    WINE_COLOUR,
    chunk_lines,
    embed,
    fill,
    fmt_nok,
    medal,
    truncate,
    wine_label,
)

log = logging.getLogger(__name__)

MIN_RATINGS_FOR_BOARD = 2
BOARD_DEFAULT_LIMIT = 10


# -- pure helpers (unit-tested in tests/test_wine.py) ------------------------


def group_average(scores: Sequence[int]) -> float | None:
    """Mean rating, or None when nobody has rated yet."""
    if not scores:
        return None
    return sum(scores) / len(scores)


def value_score(average: float | None, price_nok: int | None) -> float | None:
    """Rating points per 100 kr — higher is a better bargain.

    None when there is no rating or no usable price; a free bottle has no
    meaningful value-for-money, so 0 kr is treated as unusable rather than
    infinitely good.
    """
    if average is None or not price_nok or price_nok <= 0:
        return None
    return average * 100 / price_nok


def rating_bar(score: int, width: int = 10) -> str:
    """A coarse 1–100 score as filled blocks, for at-a-glance comparison."""
    filled = max(0, min(width, round(score / 100 * width)))
    return "█" * filled + "░" * (width - filled)


# -- cog --------------------------------------------------------------------


class Wine(commands.Cog):
    wine = app_commands.Group(name="wine", description="The FC Vino cellar.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    # -- shared lookups ----------------------------------------------------

    def _search_rows(self, term: str, limit: int = 25) -> list[sqlite3.Row]:
        like = f"%{term.strip()}%"
        return self.db.query(
            """SELECT * FROM wines
               WHERE name LIKE ? OR producer LIKE ? OR country LIKE ?
                     OR region LIKE ? OR grape LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (like, like, like, like, like, limit),
        )

    def _resolve(self, value: str) -> sqlite3.Row | None:
        """Accept an autocomplete value (the id) or whatever the user typed."""
        value = value.strip()
        if value.isdigit():
            row = self.db.query_one("SELECT * FROM wines WHERE id=?", (int(value),))
            if row is not None:
                return row
        matches = self._search_rows(value, limit=1)
        return matches[0] if matches else None

    async def bottle_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = (
            self._search_rows(current)
            if current.strip()
            else self.db.query("SELECT * FROM wines ORDER BY id DESC LIMIT 25")
        )
        return [
            app_commands.Choice(name=truncate(wine_label(r), 100), value=str(r["id"]))
            for r in rows
        ]

    def _top_rows(self, limit: int) -> list[sqlite3.Row]:
        """Bottles by group average, best first. Needs MIN_RATINGS_FOR_BOARD scores."""
        return self.db.query(
            """SELECT w.*, AVG(r.score) AS avg_score, COUNT(r.score) AS n
               FROM wines w JOIN wine_ratings r ON r.wine_id = w.id
               GROUP BY w.id HAVING n >= ?
               ORDER BY avg_score DESC, n DESC LIMIT ?""",
            (MIN_RATINGS_FOR_BOARD, limit),
        )

    def _value_rows(self, limit: int) -> list[sqlite3.Row]:
        """Bottles by rating per krone, best first. Priced bottles only."""
        return self.db.query(
            """SELECT w.*, AVG(r.score) AS avg_score, COUNT(r.score) AS n
               FROM wines w JOIN wine_ratings r ON r.wine_id = w.id
               WHERE w.price_nok IS NOT NULL AND w.price_nok > 0
               GROUP BY w.id HAVING n >= ?
               ORDER BY AVG(r.score) * 100.0 / w.price_nok DESC LIMIT ?""",
            (MIN_RATINGS_FOR_BOARD, limit),
        )

    def _rating_rows(self, wine_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM wine_ratings WHERE wine_id=? ORDER BY score DESC", (wine_id,)
        )

    # -- commands ----------------------------------------------------------

    @wine.command(name="add", description="Log a bottle so the group can rate it.")
    @app_commands.describe(
        name="Wine name, e.g. Barolo Castiglione",
        producer="Producer or château",
        vintage="Vintage year, e.g. 2018",
        country="Country of origin",
        region="Region or appellation",
        grape="Grape or blend",
        price_nok="Price in kroner",
        bought_at="Where you bought it, e.g. Vinmonopolet",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        producer: str | None = None,
        vintage: app_commands.Range[int, 1800, 2100] | None = None,
        country: str | None = None,
        region: str | None = None,
        grape: str | None = None,
        price_nok: app_commands.Range[int, 0, 1_000_000] | None = None,
        bought_at: str | None = None,
    ) -> None:
        wine_id = self.db.execute(
            """INSERT INTO wines (name, producer, vintage, country, region, grape,
                                  price_nok, bought_at, added_by, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name.strip(),
                (producer or "").strip() or None,
                vintage,
                (country or "").strip() or None,
                (region or "").strip() or None,
                (grape or "").strip() or None,
                price_nok,
                (bought_at or "").strip() or None,
                interaction.user.id,
                utcnow_iso(),
            ),
        )
        row = self.db.query_one("SELECT * FROM wines WHERE id=?", (wine_id,))
        assert row is not None

        e = embed(f"🍷 {wine_label(row)}", colour=WINE_COLOUR)
        e.add_field(name="Origin", value=self._origin(row) or "—")
        e.add_field(name="Price", value=fmt_nok(row["price_nok"]))
        if row["bought_at"]:
            e.add_field(name="Bought at", value=row["bought_at"])
        e.set_footer(text=f"Bottle #{wine_id} · rate it with /wine rate")
        await interaction.response.send_message(embed=e)
        await self._announce(interaction, e)

    @wine.command(name="rate", description="Score a bottle out of 100 and leave notes.")
    @app_commands.describe(
        bottle="Start typing the wine name",
        score="1–100",
        notes="What did it taste like?",
    )
    @app_commands.autocomplete(bottle=bottle_autocomplete)
    async def rate(
        self,
        interaction: discord.Interaction,
        bottle: str,
        score: app_commands.Range[int, 1, 100],
        notes: str | None = None,
    ) -> None:
        row = self._resolve(bottle)
        if row is None:
            await interaction.response.send_message(
                f"No bottle matching **{truncate(bottle, 80)}**. Add it with `/wine add` first.",
                ephemeral=True,
            )
            return

        previous = self.db.query_one(
            "SELECT score FROM wine_ratings WHERE wine_id=? AND user_id=?",
            (row["id"], interaction.user.id),
        )
        self.db.execute(
            """INSERT INTO wine_ratings (wine_id, user_id, score, notes, rated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(wine_id, user_id) DO UPDATE SET
                   score=excluded.score, notes=excluded.notes, rated_at=excluded.rated_at""",
            (
                row["id"],
                interaction.user.id,
                int(score),
                (notes or "").strip() or None,
                utcnow_iso(),
            ),
        )

        ratings = self._rating_rows(row["id"])
        avg = group_average([r["score"] for r in ratings])
        verb = f"updated from **{previous['score']}** to" if previous else "rated"
        summary = f"{rating_bar(int(score))} **{score}**/100"
        detail = f"Group average now **{avg:.1f}** from {len(ratings)} rating(s)."
        await interaction.response.send_message(
            f"🍷 {interaction.user.mention} {verb} **{wine_label(row)}**\n{summary}\n{detail}"
        )

    @wine.command(name="show", description="Everything the group thinks about one bottle.")
    @app_commands.describe(bottle="Start typing the wine name")
    @app_commands.autocomplete(bottle=bottle_autocomplete)
    async def show(self, interaction: discord.Interaction, bottle: str) -> None:
        row = self._resolve(bottle)
        if row is None:
            await interaction.response.send_message(
                f"No bottle matching **{truncate(bottle, 80)}**.", ephemeral=True
            )
            return

        ratings = self._rating_rows(row["id"])
        avg = group_average([r["score"] for r in ratings])
        value = value_score(avg, row["price_nok"])

        e = embed(f"🍷 {wine_label(row)}", colour=WINE_COLOUR)
        e.add_field(name="Origin", value=self._origin(row) or "—")
        e.add_field(name="Grape", value=row["grape"] or "—")
        e.add_field(name="Price", value=fmt_nok(row["price_nok"]))
        e.add_field(
            name="Group average",
            value=f"**{avg:.1f}**/100 ({len(ratings)} rating(s))" if avg else "not rated yet",
        )
        e.add_field(
            name="Value",
            value=f"{value:.1f} pts / 100 kr" if value else "—",
        )
        if row["bought_at"]:
            e.add_field(name="Bought at", value=row["bought_at"])

        if ratings:
            lines = []
            for r in ratings:
                line = f"<@{r['user_id']}> **{r['score']}** {rating_bar(r['score'])}"
                if r["notes"]:
                    line += f"\n> {truncate(r['notes'], 300)}"
                lines.append(line)
            for i, block in enumerate(chunk_lines(lines)):
                e.add_field(name="Ratings" if i == 0 else "…", value=block, inline=False)
        else:
            e.add_field(
                name="Ratings",
                value="Nobody has rated this yet. `/wine rate` is right there.",
                inline=False,
            )
        e.set_footer(text=f"Bottle #{row['id']}")
        await interaction.response.send_message(embed=e)

    @wine.command(name="top", description="Best-rated bottles in the cellar.")
    @app_commands.describe(limit="How many to show (default 10)")
    async def top(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = BOARD_DEFAULT_LIMIT,
    ) -> None:
        rows = self._top_rows(int(limit))
        if not rows:
            await interaction.response.send_message(
                f"Nothing qualifies yet — a bottle needs {MIN_RATINGS_FOR_BOARD} ratings "
                "to make the board.",
                ephemeral=True,
            )
            return
        lines = [
            f"{medal(i)} **{row['avg_score']:.1f}** · {truncate(wine_label(row), 80)} "
            f"_({row['n']} ratings, {fmt_nok(row['price_nok'])})_"
            for i, row in enumerate(rows, start=1)
        ]
        e = embed("🏆 Top of the cellar", colour=WINE_COLOUR, description="\n".join(lines))
        e.set_footer(text=f"Minimum {MIN_RATINGS_FOR_BOARD} ratings to qualify")
        await interaction.response.send_message(embed=e)

    @wine.command(name="value", description="Best rating per krone.")
    @app_commands.describe(limit="How many to show (default 10)")
    async def value(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = BOARD_DEFAULT_LIMIT,
    ) -> None:
        rows = self._value_rows(int(limit))
        if not rows:
            await interaction.response.send_message(
                "No bottles with both a price and "
                f"{MIN_RATINGS_FOR_BOARD} ratings yet.",
                ephemeral=True,
            )
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            v = value_score(row["avg_score"], row["price_nok"])
            lines.append(
                f"{medal(i)} **{v:.1f}** pts/100 kr · {truncate(wine_label(row), 70)} "
                f"_({row['avg_score']:.1f} at {fmt_nok(row['price_nok'])})_"
            )
        e = embed("💰 Best value", colour=WINE_COLOUR, description="\n".join(lines))
        e.set_footer(text="Rating points per 100 kr")
        await interaction.response.send_message(embed=e)

    @wine.command(name="search", description="Find a bottle by name, producer, country or grape.")
    @app_commands.describe(query="Anything to match on")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        rows = self._search_rows(query, limit=15)
        if not rows:
            await interaction.response.send_message(
                f"Nothing matches **{truncate(query, 80)}**.", ephemeral=True
            )
            return
        lines = []
        for row in rows:
            agg = self.db.query_one(
                "SELECT AVG(score) AS avg_score, COUNT(*) AS n FROM wine_ratings WHERE wine_id=?",
                (row["id"],),
            )
            rating = (
                f"**{agg['avg_score']:.1f}** ({agg['n']})" if agg and agg["n"] else "unrated"
            )
            lines.append(
                f"`#{row['id']}` {truncate(wine_label(row), 70)} — {rating}, "
                f"{fmt_nok(row['price_nok'])}"
            )
        e = embed(
            f"🔍 {len(rows)} match(es) for “{truncate(query, 60)}”",
            colour=WINE_COLOUR,
            description="\n".join(lines),
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @wine.command(name="mine", description="Your own ratings, newest first.")
    async def mine(self, interaction: discord.Interaction) -> None:
        rows = self.db.query(
            """SELECT w.*, r.score, r.notes, r.rated_at
               FROM wine_ratings r JOIN wines w ON w.id = r.wine_id
               WHERE r.user_id = ? ORDER BY r.rated_at DESC LIMIT 25""",
            (interaction.user.id,),
        )
        if not rows:
            await interaction.response.send_message(
                "You haven't rated anything yet. `/wine rate` to start.", ephemeral=True
            )
            return
        lines = []
        for row in rows:
            line = f"**{row['score']}** · {truncate(wine_label(row), 70)}"
            if row["notes"]:
                line += f"\n> {truncate(row['notes'], 200)}"
            lines.append(line)
        e = embed(
            "📓 Your tasting book",
            colour=WINE_COLOUR,
            description=f"**{len(rows)}** bottle(s)",
        )
        fill(e, lines)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @wine.command(name="remove", description="Delete a bottle you added by mistake.")
    @app_commands.describe(bottle="Start typing the wine name")
    @app_commands.autocomplete(bottle=bottle_autocomplete)
    async def remove(self, interaction: discord.Interaction, bottle: str) -> None:
        row = self._resolve(bottle)
        if row is None:
            await interaction.response.send_message(
                f"No bottle matching **{truncate(bottle, 80)}**.", ephemeral=True
            )
            return
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if row["added_by"] != interaction.user.id and not is_admin:
            await interaction.response.send_message(
                f"<@{row['added_by']}> added that one — ask them, or a server admin, to remove it.",
                ephemeral=True,
            )
            return
        self.db.execute("DELETE FROM wine_ratings WHERE wine_id=?", (row["id"],))
        self.db.execute("DELETE FROM wines WHERE id=?", (row["id"],))
        await interaction.response.send_message(
            f"🗑️ Removed **{wine_label(row)}** and its ratings."
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _origin(row: sqlite3.Row) -> str:
        parts = [p for p in (row["region"], row["country"]) if p]
        return ", ".join(parts)

    async def _announce(self, interaction: discord.Interaction, e: discord.Embed) -> None:
        """Mirror a new bottle into the wine channel, if one is configured elsewhere."""
        if interaction.guild is None:
            return
        channel_id = self.db.get_channel(interaction.guild.id, "wine")
        if not channel_id or channel_id == getattr(interaction.channel, "id", None):
            return
        channel = interaction.guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    content=f"New bottle in the cellar, courtesy of {interaction.user.mention}:",
                    embed=e,
                )
            except discord.HTTPException:
                log.warning("could not announce new bottle in channel %s", channel_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Wine(bot))
