"""Fixtures, results, tables — and a nudge before kickoff.

Reminders are posted from a 15-minute loop reading the local mirror, and every
(match, guild) pair is claimed in `reminders_sent` before posting, so a restart
mid-window can't double-ping.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import pinned
from ..config import FREE_COMPETITIONS
from ..db import parse_utc, sql_str_tuple, utcnow
from ..football_api import (
    FINISHED_STATUSES,
    UNPLAYED_STATUSES,
    FootballAPIError,
)
from ..formatting import (
    FOOTBALL_COLOUR,
    chunk_lines,
    embed,
    fmt_score,
    kickoff_relative,
    kickoff_ts,
    local_day,
    truncate,
)
from ..mirror import refresh_competitions, refresh_favourite_teams

log = logging.getLogger(__name__)

REFRESH_MINUTES = 15
STANDINGS_REFRESH_MINUTES = 30
# Favourite clubs get their own API call each, so refresh them less often than
# the league-wide lists: once an hour is ample for a kickoff-time change.
TEAM_REFRESH_EVERY = 4
MAX_FIXTURE_LINES = 25
MAX_TABLE_ROWS = 20

# The header and every row go through this one template, so they cannot drift
# out of alignment. Widths hold for a full season: 38 games, three-figure goal
# tallies and points, and a two-digit position for a 36-team league phase.
TEAM_COLUMN_WIDTH = 14
STANDINGS_ROW = "{pos:>2}  {team:<14} {played:>2} {gf:>3} {ga:>3} {gd:>3} {pts:>3}"
STANDINGS_HEADER = STANDINGS_ROW.format(
    pos="#", team="Team", played="P", gf="F", ga="A", gd="GD", pts="Pts"
)


def _cell(value: object) -> str:
    """A value for the table, or `-` when the API didn't send one.

    A missing number is shown as missing rather than as a fabricated 0.
    """
    return "-" if value is None else str(value)


def _signed(value: object) -> str:
    """Goal difference, signed — but plain `0` rather than `+0`."""
    if not isinstance(value, int):
        return "-"
    return f"{value:+d}" if value else "0"


COMPETITION_CHOICES = [
    app_commands.Choice(name=name, value=code) for code, name in FREE_COMPETITIONS.items()
]


class Football(commands.Cog):
    football = app_commands.Group(name="football", description="Fixtures, results and tables.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._tick = 0

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    @property
    def api(self):
        return self.bot.football  # type: ignore[attr-defined]

    @property
    def cfg(self):
        return self.bot.cfg  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.reminder_loop.start()
        self.standings_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.standings_loop.cancel()

    # -- mirror ------------------------------------------------------------

    def _team_codes(self) -> list[str]:
        """Competitions we offer as a source of registerable clubs."""
        codes = list(self.cfg.reminder_competitions)
        for extra in ("PL", "CL"):
            if extra not in codes:
                codes.append(extra)
        return codes

    async def _refresh(self, codes: list[str] | tuple[str, ...], *, teams: bool = False) -> None:
        await refresh_competitions(self.api, self.db, codes)
        if teams:
            await refresh_favourite_teams(self.api, self.db)

    # -- queries against the mirror ---------------------------------------

    def _upcoming(self, codes: list[str], *, days: int, team_ids: list[int]) -> list:
        now = utcnow()
        lo = now.isoformat(timespec="seconds")
        hi = (now + timedelta(days=days)).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(codes)) or "NULL"
        team_clause = ""
        params: list = [lo, hi, *codes]
        if team_ids:
            marks = ",".join("?" * len(team_ids))
            team_clause = f" OR home_id IN ({marks}) OR away_id IN ({marks})"
            params += team_ids + team_ids
        return self.db.query(
            f"""SELECT * FROM matches
                WHERE kickoff_utc BETWEEN ? AND ?
                  AND status IN {sql_str_tuple(UNPLAYED_STATUSES)}
                  AND (competition IN ({placeholders}){team_clause})
                ORDER BY kickoff_utc LIMIT {MAX_FIXTURE_LINES}""",
            params,
        )

    def _recent(self, codes: list[str], *, days: int, team_ids: list[int]) -> list:
        now = utcnow()
        lo = (now - timedelta(days=days)).isoformat(timespec="seconds")
        hi = now.isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(codes)) or "NULL"
        team_clause = ""
        params: list = [lo, hi, *codes]
        if team_ids:
            marks = ",".join("?" * len(team_ids))
            team_clause = f" OR home_id IN ({marks}) OR away_id IN ({marks})"
            params += team_ids + team_ids
        return self.db.query(
            f"""SELECT * FROM matches
                WHERE kickoff_utc BETWEEN ? AND ?
                  AND status IN {sql_str_tuple(FINISHED_STATUSES)}
                  AND (competition IN ({placeholders}){team_clause})
                ORDER BY kickoff_utc DESC LIMIT {MAX_FIXTURE_LINES}""",
            params,
        )

    # -- rendering ---------------------------------------------------------

    def _fixture_lines(self, rows, *, with_score: bool) -> list[str]:
        lines: list[str] = []
        current_day = None
        for row in rows:
            kickoff = parse_utc(row["kickoff_utc"])
            day = local_day(kickoff, self.cfg.tz)
            if day != current_day:
                lines.append(f"\n**{day}**")
                current_day = day
            tag = f"`{row['competition']}`"
            if with_score:
                lines.append(
                    f"{tag} **{row['home']}** {fmt_score(row['home_goals'], row['away_goals'])}"
                    f" **{row['away']}**"
                )
            else:
                lines.append(
                    f"{kickoff_ts(kickoff, 't')} {tag} {row['home']} vs {row['away']}"
                )
        return [line for line in lines if line]

    async def _send_list(
        self,
        interaction: discord.Interaction,
        rows,
        *,
        title: str,
        with_score: bool,
        empty: str,
    ) -> None:
        if not rows:
            await interaction.followup.send(empty, ephemeral=True)
            return
        e = embed(title, colour=FOOTBALL_COLOUR)
        blocks = chunk_lines(self._fixture_lines(rows, with_score=with_score))
        for i, block in enumerate(blocks):
            e.add_field(name="​" if i else "​", value=block, inline=False)
        await interaction.followup.send(embed=e)

    # -- commands ----------------------------------------------------------

    @football.command(name="fixtures", description="What's coming up.")
    @app_commands.describe(
        competition="Leave empty for the competitions we follow plus your club",
        days="How far ahead to look (default 7)",
    )
    @app_commands.choices(competition=COMPETITION_CHOICES)
    async def fixtures(
        self,
        interaction: discord.Interaction,
        competition: app_commands.Choice[str] | None = None,
        days: app_commands.Range[int, 1, 14] = 7,
    ) -> None:
        await interaction.response.defer()
        codes = [competition.value] if competition else list(self.cfg.reminder_competitions)
        team_ids = self._own_team_ids(interaction) if competition is None else []
        try:
            await self._refresh(codes, teams=bool(team_ids))
        except FootballAPIError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        rows = self._upcoming(codes, days=int(days), team_ids=team_ids)
        label = competition.name if competition else "Coming up"
        await self._send_list(
            interaction,
            rows,
            title=f"⚽ {label} — next {int(days)} day(s)",
            with_score=False,
            empty=f"Nothing scheduled in the next {int(days)} day(s).",
        )

    @football.command(name="results", description="Recent scores.")
    @app_commands.describe(
        competition="Leave empty for the competitions we follow plus your club",
        days="How far back to look (default 3)",
    )
    @app_commands.choices(competition=COMPETITION_CHOICES)
    async def results(
        self,
        interaction: discord.Interaction,
        competition: app_commands.Choice[str] | None = None,
        days: app_commands.Range[int, 1, 14] = 3,
    ) -> None:
        await interaction.response.defer()
        codes = [competition.value] if competition else list(self.cfg.reminder_competitions)
        team_ids = self._own_team_ids(interaction) if competition is None else []
        try:
            await self._refresh(codes, teams=bool(team_ids))
        except FootballAPIError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        rows = self._recent(codes, days=int(days), team_ids=team_ids)
        label = competition.name if competition else "Results"
        await self._send_list(
            interaction,
            rows,
            title=f"📋 {label} — last {int(days)} day(s)",
            with_score=True,
            empty=f"No finished matches in the last {int(days)} day(s).",
        )

    @football.command(name="standings", description="League table.")
    @app_commands.choices(competition=COMPETITION_CHOICES)
    async def standings(
        self,
        interaction: discord.Interaction,
        competition: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        code = competition.value if competition else self.cfg.prediction_competition
        name = competition.name if competition else FREE_COMPETITIONS.get(code, code)
        try:
            data = await self.api.standings(code)
        except FootballAPIError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        e = self._standings_embed(data, name)
        if e is None:
            await interaction.followup.send(
                f"No table published for {name} yet.", ephemeral=True
            )
            return
        await interaction.followup.send(embed=e)

    @football.command(name="myteam", description="Register your club so you get pinged for it.")
    @app_commands.describe(club="Start typing your club; leave empty to see your current pick")
    async def myteam(self, interaction: discord.Interaction, club: str | None = None) -> None:
        if club is None:
            row = self.db.query_one(
                "SELECT favourite_team_name FROM members WHERE user_id=?", (interaction.user.id,)
            )
            current = row["favourite_team_name"] if row else None
            message = (
                f"You follow **{current}**. `/football myteam` with a club to change it."
                if current
                else "You haven't picked a club. `/football myteam` and start typing one."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        if not club.strip().isdigit():
            await interaction.response.send_message(
                "Pick a club from the suggestions rather than typing it freehand — "
                "that's how I get the right team id.",
                ephemeral=True,
            )
            return

        team_id = int(club)
        await interaction.response.defer(ephemeral=True)
        try:
            teams = await self.api.selectable_teams(self._team_codes())
        except FootballAPIError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        match = next((t for t in teams if t["id"] == team_id), None)
        if match is None:
            await interaction.followup.send(
                "I don't recognise that club. Try the suggestions again.", ephemeral=True
            )
            return
        self.db.set_favourite_team(interaction.user.id, team_id, match["full_name"])
        await interaction.followup.send(
            f"✅ You now follow **{match['full_name']}**. "
            "You'll be tagged before their kickoffs once a football channel is set.",
            ephemeral=True,
        )

    @myteam.autocomplete("club")
    async def club_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            teams = await self.api.selectable_teams(self._team_codes())
        except FootballAPIError:
            return []
        term = current.strip().lower()
        matches = [
            t
            for t in teams
            if not term
            or term in (t["full_name"] or "").lower()
            or term in (t["name"] or "").lower()
        ]
        return [
            app_commands.Choice(name=truncate(t["full_name"] or t["name"], 100), value=str(t["id"]))
            for t in matches[:25]
        ]

    @football.command(name="forget-team", description="Stop following your club.")
    async def forget_team(self, interaction: discord.Interaction) -> None:
        self.db.set_favourite_team(interaction.user.id, None, None)
        await interaction.response.send_message(
            "Done — no more reminders for your club.", ephemeral=True
        )

    # -- standings rendering -----------------------------------------------

    @staticmethod
    def _standings_tables(data: dict) -> list[dict]:
        """The overall tables in an API standings payload, groups included."""
        return [s for s in data.get("standings", []) if s.get("type") == "TOTAL"]

    def _standings_line(self, row: dict) -> str:
        """One table row, padded to the same width as every other."""
        return STANDINGS_ROW.format(
            pos=_cell(row.get("position")),
            team=truncate(self._team_name(row), TEAM_COLUMN_WIDTH),
            played=_cell(row.get("playedGames")),
            gf=_cell(row.get("goalsFor")),
            ga=_cell(row.get("goalsAgainst")),
            gd=_signed(row.get("goalDifference")),
            pts=_cell(row.get("points")),
        )

    def _standings_embed(self, data: dict, name: str) -> discord.Embed | None:
        """Render a standings payload, or None if no table is published yet.

        The whole table goes in one code block. Discord renders ordinary message
        text in a proportional font and collapses runs of spaces, so column
        padding only survives inside a code block — the previous version padded
        the club names in plain text, which is why the rows never lined up.
        """
        totals = self._standings_tables(data)
        if not totals:
            return None
        e = embed(f"🏟️ {name}", colour=FOOTBALL_COLOUR)
        for standing in totals[:4]:
            table = standing.get("table", [])[:MAX_TABLE_ROWS]
            lines = [self._standings_line(row) for row in table]
            if not lines:
                continue
            header = standing.get("group") or "Table"
            body = "\n".join([STANDINGS_HEADER, *lines])
            e.add_field(
                name=header.replace("_", " ").title(),
                value=f"```\n{body}\n```",
                inline=False,
            )
        if not e.fields:
            return None
        e.set_footer(text=f"Top {MAX_TABLE_ROWS} shown · football-data.org")
        return e

    def _standings_hash(self, data: dict) -> str:
        """Fingerprint of the table's substance, ignoring anything cosmetic.

        The loop compares this before editing, so a table that hasn't moved
        since the last check is left alone rather than re-edited every half
        hour — no "(edited)" marks on a quiet Tuesday.

        It has to cover every column the table shows. Goals for and against are
        in here because two teams drawing 1-1 and drawing 2-2 come out with the
        same points and the same goal difference: fingerprint only those and the
        goals columns would sit there stale.
        """
        parts: list[str] = []
        for standing in self._standings_tables(data):
            parts.append(str(standing.get("group") or ""))
            for row in standing.get("table", [])[:MAX_TABLE_ROWS]:
                parts.append(
                    f"{row.get('position')}|{self._team_name(row)}|{row.get('playedGames')}"
                    f"|{row.get('goalsFor')}|{row.get('goalsAgainst')}"
                    f"|{row.get('goalDifference')}|{row.get('points')}"
                )
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    # -- standings loop ----------------------------------------------------

    @tasks.loop(minutes=STANDINGS_REFRESH_MINUTES)
    async def standings_loop(self) -> None:
        """Keep one message per guild showing the current table.

        A channel cannot invoke a slash command, so a dedicated table channel is
        the bot posting once and editing that message from then on.
        """
        targets = self.db.configured_guilds("standings")
        if not targets:
            return
        code = self.cfg.prediction_competition
        name = FREE_COMPETITIONS.get(code, code)
        try:
            data = await self.api.standings(code)
        except FootballAPIError as exc:
            log.warning("standings refresh failed: %s", exc)
            return

        e = self._standings_embed(data, name)
        if e is None:
            log.info("no %s table published yet", code)
            return

        await pinned.publish(
            self.bot,
            "standings",
            embed=e,
            fingerprint=self._standings_hash(data),
            pin_reason="FC Vino league table",
        )

    @standings_loop.before_loop
    async def before_standings_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -- reminder loop -----------------------------------------------------

    @tasks.loop(minutes=REFRESH_MINUTES)
    async def reminder_loop(self) -> None:
        self._tick += 1
        include_teams = self._tick % TEAM_REFRESH_EVERY == 1
        try:
            await self._refresh(self.cfg.reminder_competitions, teams=include_teams)
        except FootballAPIError as exc:
            log.warning("mirror refresh failed: %s", exc)

        targets = self.db.configured_guilds("football")
        if not targets:
            return

        followers = self.db.favourite_teams()
        now = utcnow()
        lead = timedelta(minutes=self.cfg.reminder_lead_minutes)
        rows = self.db.query(
            f"""SELECT * FROM matches
                WHERE status IN {sql_str_tuple(UNPLAYED_STATUSES)}
                  AND kickoff_utc BETWEEN ? AND ?
                ORDER BY kickoff_utc""",
            (now.isoformat(timespec="seconds"), (now + lead).isoformat(timespec="seconds")),
        )
        for guild_id, channel_id in targets:
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if not isinstance(channel, discord.TextChannel):
                continue
            for row in rows:
                if not self._is_relevant(row, followers):
                    continue
                if not self.db.claim_reminder(row["match_id"], f"kickoff:{guild_id}"):
                    continue
                try:
                    await channel.send(**self._reminder_payload(row, followers))
                except discord.HTTPException:
                    log.exception("failed to post reminder for match %s", row["match_id"])

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    def _is_relevant(self, row, followers: dict[int, list[int]]) -> bool:
        if row["competition"] in self.cfg.reminder_competitions:
            return True
        return row["home_id"] in followers or row["away_id"] in followers

    def _reminder_payload(self, row, followers: dict[int, list[int]]) -> dict:
        kickoff = parse_utc(row["kickoff_utc"])
        competition = FREE_COMPETITIONS.get(row["competition"], row["competition"])
        e = embed(
            f"⏰ {row['home']} vs {row['away']}",
            colour=FOOTBALL_COLOUR,
            description=(
                f"{competition}\nKickoff {kickoff_ts(kickoff)} — {kickoff_relative(kickoff)}"
            ),
        )
        interested = sorted(
            {uid for tid in (row["home_id"], row["away_id"]) for uid in followers.get(tid, [])}
        )
        content = " ".join(f"<@{uid}>" for uid in interested) or None
        return {"content": content, "embed": e}

    # -- helpers -----------------------------------------------------------

    def _own_team_ids(self, interaction: discord.Interaction) -> list[int]:
        row = self.db.query_one(
            "SELECT favourite_team_id FROM members WHERE user_id=?", (interaction.user.id,)
        )
        return [row["favourite_team_id"]] if row and row["favourite_team_id"] else []

    @staticmethod
    def _team_name(row: dict) -> str:
        team = row.get("team") or {}
        return team.get("shortName") or team.get("tla") or team.get("name") or "?"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Football(bot))
