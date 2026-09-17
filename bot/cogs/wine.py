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

from .. import wine_stats as stats
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

# Kept as a name here because the cog says it in half a dozen user-facing
# sentences; the number itself is wine_stats.MIN_RATINGS, shared with the web
# app so the two halves cannot disagree about what qualifies.
MIN_RATINGS_FOR_BOARD = stats.MIN_RATINGS
BOARD_DEFAULT_LIMIT = 10

# The origin boards live in a code block, so every row has to be the same width
# in a monospace font — Discord collapses runs of spaces everywhere else.
ORIGIN_NAME_WIDTH = 22   # fits "Brunello di Montalcino" and "Touriga Nacional blend"
ORIGIN_ROW = "{rank:>2}  {name:<22} {wines:>4}{mark:<1} {avg:>5}"
MAX_ORIGIN_ROWS = 20

# A bucket whose bottles were all poured on one night says as much about that
# night as about the grape, so it is marked rather than quietly ranked.
ONE_EVENING_MARK = "*"

# Which columns can be ranked, and what to call one in a sentence. Also the
# allow-list for the column name interpolated into _origin_rows' SQL.
ORIGIN_FIELDS = {"country": "country", "region": "region", "grape": "grape"}


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
        """A wine's ratings with the rater's name and Discord id, best first."""
        return self.db.query(
            """SELECT r.*, m.name AS member_name, m.discord_id
               FROM wine_ratings r JOIN wine_members m ON m.id = r.member_id
               WHERE r.wine_id=? ORDER BY r.score DESC""",
            (wine_id,),
        )

    def _member_for(self, user: discord.abc.User) -> int:
        """The wine_members row for a Discord user, creating one if needed.

        Everyone in the spreadsheet already has a row; `/wine iam` is what
        attaches a Discord account to it. Someone who rates a bottle without
        having claimed a name gets their own row, so their score is never lost.
        """
        row = self.db.query_one("SELECT id FROM wine_members WHERE discord_id=?", (user.id,))
        if row:
            return row["id"]
        name = getattr(user, "display_name", None) or f"discord:{user.id}"
        self.db.execute(
            """INSERT INTO wine_members (name, discord_id) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET discord_id=excluded.discord_id""",
            (name, user.id),
        )
        return self.db.query_one("SELECT id FROM wine_members WHERE discord_id=?", (user.id,))["id"]

    @staticmethod
    def _rater(row: sqlite3.Row) -> str:
        """How to address a rater: a mention once they've claimed their name."""
        if row["discord_id"]:
            return f"<@{row['discord_id']}>"
        return f"**{row['member_name']}**"

    def _tasting_for(self, wine_id: int) -> sqlite3.Row | None:
        return self.db.query_one(
            """SELECT t.* FROM tastings t JOIN wines w ON w.tasting_id = t.id
               WHERE w.id=?""",
            (wine_id,),
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

    @wine.command(name="iam", description="Claim your name from the club's records.")
    @app_commands.describe(name="Your name as it appears in the tasting sheet")
    async def iam(self, interaction: discord.Interaction, name: str) -> None:
        """Attach a Discord account to a name from the spreadsheet.

        Thirteen years of scores arrived keyed on first names. This is how those
        become *your* scores, so `/wine mine` and the boards can address you.
        """
        row = self.db.query_one(
            "SELECT * FROM wine_members WHERE LOWER(name)=LOWER(?)", (name.strip(),)
        )
        if row is None:
            known = ", ".join(
                r["name"] for r in self.db.query("SELECT name FROM wine_members ORDER BY name")
            )
            await interaction.response.send_message(
                f"No **{truncate(name, 40)}** in the records. Known names: {known}",
                ephemeral=True,
            )
            return
        if row["discord_id"] and row["discord_id"] != interaction.user.id:
            await interaction.response.send_message(
                f"**{row['name']}** is already claimed by <@{row['discord_id']}>.", ephemeral=True
            )
            return

        # Free the name from any row this user claimed before, so one account
        # can't end up attached to two of them.
        self.db.execute(
            "UPDATE wine_members SET discord_id=NULL WHERE discord_id=? AND id<>?",
            (interaction.user.id, row["id"]),
        )
        self.db.execute(
            "UPDATE wine_members SET discord_id=? WHERE id=?", (interaction.user.id, row["id"])
        )
        count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM wine_ratings WHERE member_id=?", (row["id"],)
        )
        await interaction.response.send_message(
            f"🍷 You are **{row['name']}** — {count['n']} ratings are yours.", ephemeral=True
        )

    @iam.autocomplete("name")
    async def iam_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        term = current.strip().lower()
        rows = self.db.query(
            """SELECT m.name, m.discord_id, COUNT(r.score) AS n FROM wine_members m
               LEFT JOIN wine_ratings r ON r.member_id = m.id
               GROUP BY m.id ORDER BY n DESC""",
        )
        return [
            app_commands.Choice(
                name=truncate(
                    f"{r['name']} — {r['n']} ratings" + (" (claimed)" if r["discord_id"] else ""),
                    100,
                ),
                value=r["name"],
            )
            for r in rows
            if not term or term in r["name"].lower()
        ][:25]

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

        member_id = self._member_for(interaction.user)
        previous = self.db.query_one(
            "SELECT score FROM wine_ratings WHERE wine_id=? AND member_id=?",
            (row["id"], member_id),
        )
        self.db.execute(
            """INSERT INTO wine_ratings (wine_id, member_id, score, notes, rated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(wine_id, member_id) DO UPDATE SET
                   score=excluded.score, notes=excluded.notes, rated_at=excluded.rated_at""",
            (
                row["id"],
                member_id,
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
        tasting = self._tasting_for(row["id"])
        if tasting:
            when = f"{tasting['year']}"
            if tasting["month"]:
                when = f"{tasting['year']}-{tasting['month']:02d}"
            where = f" at {tasting['location']}" if tasting["location"] else ""
            theme = f" · {tasting['theme']}" if tasting["theme"] else ""
            e.add_field(name="Tasting", value=f"{when}{theme}{where}", inline=False)
        if row["brought_by"]:
            e.add_field(name="Brought by", value=row["brought_by"])

        if ratings:
            lines = []
            for r in ratings:
                line = f"{self._rater(r)} {r['score']} {rating_bar(r['score'])}"
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

    # -- what the cellar is made of ----------------------------------------

    def _origin_rows(self, field: str) -> list[sqlite3.Row]:
        """One row per rating, carrying the wine's `field` as the bucket to group on.

        The field name is interpolated because SQLite can't parameterise a column,
        so it is checked against ORIGIN_FIELDS first and never comes from the user
        as free text.
        """
        assert field in ORIGIN_FIELDS
        return self.db.query(
            f"""SELECT w.{field} AS key, w.id AS wine_id, w.tasting_id AS tasting_id,
                       r.score AS score
                FROM wines w JOIN wine_ratings r ON r.wine_id = w.id
                WHERE w.{field} IS NOT NULL"""
        )

    def _origin_embed(self, field: str, title: str, label: str) -> discord.Embed | None:
        tallies = stats.tally(self._origin_rows(field))
        if not tallies:
            return None

        shown = tallies[:MAX_ORIGIN_ROWS]
        lines = [ORIGIN_ROW.format(rank="", name=label, wines="btl", mark="", avg="avg")]
        for i, t in enumerate(shown, start=1):
            lines.append(
                ORIGIN_ROW.format(
                    rank=i,
                    name=truncate(t.name, ORIGIN_NAME_WIDTH),
                    wines=t.wines,
                    mark=ONE_EVENING_MARK if t.one_evening else "",
                    avg=f"{t.average:.1f}",
                )
            )
        e = embed(title, colour=WINE_COLOUR)
        fill(e, ["```", *lines, "```"])

        footer = f"{len(tallies)} with {stats.MIN_WINES}+ bottles"
        if len(shown) < len(tallies):
            footer += f" · top {len(shown)} of {len(tallies)}"
        gap = stats.spread(tallies)
        if gap is not None:
            footer += f" · {gap:.1f} points across all {len(tallies)}"
        if any(t.one_evening for t in shown):
            footer += f"\n{ONE_EVENING_MARK} every bottle from one evening"
        e.set_footer(text=footer)
        return e

    async def _send_origin_board(
        self, interaction: discord.Interaction, field: str, title: str
    ) -> None:
        e = self._origin_embed(field, title, ORIGIN_FIELDS[field])
        if e is None:
            await interaction.response.send_message(
                f"Nothing to rank yet — a {field} needs {stats.MIN_WINES} rated bottles "
                "before it makes the board.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=e)

    @wine.command(name="countries", description="How the club rates each country.")
    async def countries(self, interaction: discord.Interaction) -> None:
        await self._send_origin_board(interaction, "country", "🌍 By country")

    @wine.command(name="regions", description="How the club rates each region.")
    async def regions(self, interaction: discord.Interaction) -> None:
        await self._send_origin_board(interaction, "region", "🗺️ By region")

    @wine.command(name="grapes", description="How the club rates each grape.")
    async def grapes(self, interaction: discord.Interaction) -> None:
        await self._send_origin_board(interaction, "grape", "🍇 By grape")

    @wine.command(name="stats", description="The cellar in numbers.")
    async def cellar_stats(self, interaction: discord.Interaction) -> None:
        totals = self.db.query_one(
            """SELECT (SELECT COUNT(*) FROM wines) AS wines,
                      (SELECT COUNT(*) FROM wine_ratings) AS ratings,
                      (SELECT COUNT(*) FROM tastings) AS tastings,
                      (SELECT COUNT(*) FROM wine_members) AS members"""
        )
        if totals is None or not totals["wines"]:
            await interaction.response.send_message(
                "The cellar is empty. `/wine add` to put something in it.", ephemeral=True
            )
            return

        e = embed("🍷 The cellar", colour=WINE_COLOUR)
        headline = [
            f"**{totals['wines']}** bottles across **{totals['tastings']}** tastings",
            f"**{totals['ratings']}** ratings from **{totals['members']}** of us",
        ]
        span = self.db.query_one("SELECT MIN(year) AS first, MAX(year) AS last FROM tastings")
        if span and span["first"]:
            headline.append(f"tasting together since **{span['first']}**")
        lines = [" · ".join(headline), ""]

        for field, title in ORIGIN_FIELDS.items():
            board = stats.tally(self._origin_rows(field))
            if board:
                best = board[0]
                lines.append(
                    f"**Best {title}** — {best.name} at {best.average:.1f} "
                    f"_({best.wines} bottles)_"
                )

        known = stats.coverage(
            self.db.query("SELECT country, region, grape, vintage FROM wines"),
            list(ORIGIN_FIELDS) + ["vintage"],
        )
        lines.append("")
        lines.append(
            "Placed: " + " · ".join(f"{c.field} {c.share:.0%}" for c in known)
        )
        fill(e, lines)
        e.set_footer(text="/wine countries · /wine regions · /wine grapes for the full boards")
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
               WHERE r.member_id = ? ORDER BY r.rated_at DESC LIMIT 25""",
            (self._member_for(interaction.user),),
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
