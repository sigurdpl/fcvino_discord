"""Always-on commands: liveness, help, and telling the bot where to post."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..db import CHANNEL_KINDS
from ..formatting import NEUTRAL_COLOUR, embed

log = logging.getLogger(__name__)

CHANNEL_PURPOSE = {
    "football": "kickoff reminders",
    "wine": "wine announcements",
    "predictions": "matchweek fixtures, results and the leaderboard",
    "standings": "the league table, kept up to date in one message",
    "trips": "the trips archive, kept up to date in one message",
}


class Core(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check that the bot is awake.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"🍷⚽ Up and pouring. Gateway latency {latency_ms} ms."
        )

    @app_commands.command(name="fcvino-help", description="What this bot can do.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg  # type: ignore[attr-defined]
        db = self.bot.db  # type: ignore[attr-defined]

        e = embed(
            "FC Vino bot",
            colour=NEUTRAL_COLOUR,
            description="Nine friends, two obsessions. Everything runs on slash commands.",
        )
        e.add_field(
            name="🍷 Wine",
            value=(
                "`/wine add` — log a bottle\n"
                "`/wine iam` — claim your name from the club's records\n"
                "`/wine rate` — your score out of 100 plus tasting notes\n"
                "`/wine show` — group average, everyone's notes\n"
                "`/wine top` — best-rated bottles\n"
                "`/wine stats` — the cellar in numbers\n"
                "`/wine countries` · `/wine regions` · `/wine grapes` — how we rate each\n"
                "`/wine value` — best rating per krone\n"
                "`/wine search` — find a bottle\n"
                "`/wine mine` — your own ratings"
            ),
            inline=False,
        )
        e.add_field(
            name="✈️ Trips",
            value=(
                "`/trips add` — record a year's trip\n"
                "`/trips add-match` — the match we saw\n"
                "`/trips add-goals` — who scored\n"
                "`/trips list` — every trip in order\n"
                "`/trips matches` — every match as a table\n"
                "`/trips show` — one trip in full\n"
                "`/trips stats` — countries, goals, streaks\n"
                "`/trips countries` · `/trips teams` — where and who\n"
                "`/trips search` · `/trips random` · `/trips missing`"
            ),
            inline=False,
        )
        if cfg.has_football:
            e.add_field(
                name="⚽ Football",
                value=(
                    "`/football fixtures` — what's coming up\n"
                    "`/football results` — recent scores\n"
                    "`/football standings` — league table\n"
                    "`/football myteam` — register your club so you get pinged for it\n"
                    "`/football forget-team` — stop following it"
                ),
                inline=False,
            )
            e.add_field(
                name="🎯 Predictions",
                value=(
                    f"`/predict score` — exact score for a {cfg.prediction_competition} fixture "
                    "(3 pts spot on, 1 pt right result)\n"
                    "`/predict fixtures` — this matchweek and your picks\n"
                    "`/predict mine` — what you've submitted, what's missing\n"
                    "`/predict table` — the leaderboard"
                ),
                inline=False,
            )
        else:
            e.add_field(
                name="⚽ Football & predictions — off",
                value=(
                    "No `FOOTBALL_DATA_TOKEN` in `.env`. Get a free key at "
                    "football-data.org/client/register, add it, and restart the bot."
                ),
                inline=False,
            )

        if interaction.guild is not None:
            lines = []
            for kind in CHANNEL_KINDS:
                cid = db.get_channel(interaction.guild.id, kind)
                target = f"<#{cid}>" if cid else "_not set_"
                lines.append(f"**{kind}** → {target} ({CHANNEL_PURPOSE[kind]})")
            e.add_field(
                name="📣 Where the bot posts on its own",
                value="\n".join(lines) + "\n\nSet these with `/fcvino-setup`.",
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(
        name="fcvino-setup",
        description="Choose which channel the bot posts reminders and results in.",
    )
    @app_commands.describe(
        kind="Which kind of automatic post to route",
        channel="Target channel (defaults to the one you're in)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="football — kickoff reminders", value="football"),
            app_commands.Choice(name="wine — wine announcements", value="wine"),
            app_commands.Choice(name="predictions — fixtures & leaderboard", value="predictions"),
            app_commands.Choice(name="standings — a live league table", value="standings"),
            app_commands.Choice(name="trips — the away-trip archive", value="trips"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_channel(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a normal text channel for that.", ephemeral=True
            )
            return

        me = interaction.guild.me
        if me is not None:
            perms = target.permissions_for(me)
            missing = [
                label
                for label, ok in (
                    ("Send Messages", perms.send_messages),
                    ("Embed Links", perms.embed_links),
                )
                if not ok
            ]
            if missing:
                await interaction.response.send_message(
                    f"I can't post in {target.mention} — missing: {', '.join(missing)}. "
                    "Fix that in the channel's permissions and run this again.",
                    ephemeral=True,
                )
                return

        self.bot.db.set_channel(interaction.guild.id, kind.value, target.id)  # type: ignore[attr-defined]
        log.info("guild %s: %s channel set to %s", interaction.guild.id, kind.value, target.id)
        await interaction.response.send_message(
            f"✅ {CHANNEL_PURPOSE[kind.value].capitalize()} will go to {target.mention}."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Core(bot))
