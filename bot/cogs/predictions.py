"""The prediction game: exact scores across a matchweek.

3 points for the exact score, 1 for the right result, 0 otherwise.

Predictions stay hidden until kickoff — confirmations are ephemeral and
`/predict fixtures` shows only your own picks, so nobody can copy. Everything is
revealed in the matchweek wrap-up once the games have been played.

Each fixture locks at its own kickoff rather than at the start of the matchweek,
so a Sunday game stays open after Saturday's early kickoff.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..config import FREE_COMPETITIONS
from ..db import parse_utc, sql_str_tuple, utcnow, utcnow_iso
from ..football_api import DEAD_STATUSES, FINISHED_STATUSES, UNPLAYED_STATUSES, FootballAPIError
from ..formatting import (
    TABLE_COLOUR,
    chunk_lines,
    embed,
    fmt_score,
    kickoff_ts,
    local_day,
    medal,
    truncate,
)
from ..mirror import refresh_competitions

log = logging.getLogger(__name__)

POINTS_EXACT = 3
POINTS_OUTCOME = 1
SCORE_LOOP_MINUTES = 10
ANNOUNCE_WITHIN_DAYS = 7
MAX_GOALS = 20


# -- pure scoring (unit-tested in tests/test_predictions.py) -----------------


def outcome(home_goals: int, away_goals: int) -> int:
    """1 home win, 0 draw, -1 away win."""
    if home_goals > away_goals:
        return 1
    if home_goals < away_goals:
        return -1
    return 0


def score_prediction(
    pred_home: int, pred_away: int, actual_home: int, actual_away: int
) -> int:
    if (pred_home, pred_away) == (actual_home, actual_away):
        return POINTS_EXACT
    if outcome(pred_home, pred_away) == outcome(actual_home, actual_away):
        return POINTS_OUTCOME
    return 0


class Predictions(commands.Cog):
    predict = app_commands.Group(name="predict", description="The FC Vino prediction game.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    @property
    def api(self):
        return self.bot.football  # type: ignore[attr-defined]

    @property
    def cfg(self):
        return self.bot.cfg  # type: ignore[attr-defined]

    @property
    def competition(self) -> str:
        return self.cfg.prediction_competition

    @property
    def competition_name(self) -> str:
        return FREE_COMPETITIONS.get(self.competition, self.competition)

    async def cog_load(self) -> None:
        self.score_loop.start()

    async def cog_unload(self) -> None:
        self.score_loop.cancel()

    # -- mirror queries ----------------------------------------------------

    def _open_matches(self, limit: int = 25) -> list:
        return self.db.query(
            f"""SELECT * FROM matches
                WHERE competition=? AND status IN {sql_str_tuple(UNPLAYED_STATUSES)}
                  AND kickoff_utc > ?
                ORDER BY kickoff_utc LIMIT ?""",
            (self.competition, utcnow_iso(), limit),
        )

    def _matchday_rows(self, matchday: int) -> list:
        return self.db.query(
            "SELECT * FROM matches WHERE competition=? AND matchday=? ORDER BY kickoff_utc",
            (self.competition, matchday),
        )

    def _current_matchday(self) -> int | None:
        """The matchweek people should be predicting: the next one with unplayed games."""
        row = self.db.query_one(
            f"""SELECT matchday FROM matches
                WHERE competition=? AND matchday IS NOT NULL
                  AND status IN {sql_str_tuple(UNPLAYED_STATUSES)}
                ORDER BY kickoff_utc LIMIT 1""",
            (self.competition,),
        )
        if row:
            return row["matchday"]
        row = self.db.query_one(
            "SELECT MAX(matchday) AS md FROM matches WHERE competition=?", (self.competition,)
        )
        return row["md"] if row else None

    def _my_predictions(self, user_id: int, match_ids: list[int]) -> dict[int, tuple[int, int]]:
        if not match_ids:
            return {}
        marks = ",".join("?" * len(match_ids))
        rows = self.db.query(
            f"""SELECT match_id, home_goals, away_goals FROM predictions
                WHERE user_id=? AND match_id IN ({marks})""",
            [user_id, *match_ids],
        )
        return {r["match_id"]: (r["home_goals"], r["away_goals"]) for r in rows}

    def _prediction_counts(self, match_ids: list[int]) -> dict[int, int]:
        if not match_ids:
            return {}
        marks = ",".join("?" * len(match_ids))
        rows = self.db.query(
            f"""SELECT match_id, COUNT(*) AS n FROM predictions
                WHERE match_id IN ({marks}) GROUP BY match_id""",
            match_ids,
        )
        return {r["match_id"]: r["n"] for r in rows}

    # -- commands ----------------------------------------------------------

    @predict.command(name="score", description="Predict the exact score of a fixture.")
    @app_commands.describe(
        match="Start typing a team name", home="Home goals", away="Away goals"
    )
    async def score(
        self,
        interaction: discord.Interaction,
        match: str,
        home: app_commands.Range[int, 0, MAX_GOALS],
        away: app_commands.Range[int, 0, MAX_GOALS],
    ) -> None:
        if not match.strip().isdigit():
            await interaction.response.send_message(
                "Pick a fixture from the suggestions so I know which match you mean.",
                ephemeral=True,
            )
            return
        match_id = int(match)
        row = self.db.query_one("SELECT * FROM matches WHERE match_id=?", (match_id,))
        if row is None:
            await interaction.response.send_message(
                "I don't have that fixture. Try `/predict fixtures` first.", ephemeral=True
            )
            return

        kickoff = parse_utc(row["kickoff_utc"])
        if kickoff <= utcnow() or row["status"] not in UNPLAYED_STATUSES:
            await interaction.response.send_message(
                f"**{row['home']} vs {row['away']}** is locked — it kicked off "
                f"{kickoff_ts(kickoff, 'R')}.",
                ephemeral=True,
            )
            return

        existing = self.db.query_one(
            "SELECT home_goals, away_goals FROM predictions WHERE match_id=? AND user_id=?",
            (match_id, interaction.user.id),
        )
        self.db.execute(
            """INSERT INTO predictions (match_id, user_id, home_goals, away_goals, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(match_id, user_id) DO UPDATE SET
                   home_goals=excluded.home_goals,
                   away_goals=excluded.away_goals,
                   created_at=excluded.created_at""",
            (match_id, interaction.user.id, int(home), int(away), utcnow_iso()),
        )
        prefix = (
            f"Changed from {existing['home_goals']}–{existing['away_goals']} to"
            if existing
            else "Locked in:"
        )
        await interaction.response.send_message(
            f"🎯 {prefix} **{row['home']} {int(home)}–{int(away)} {row['away']}**\n"
            f"Editable until kickoff, {kickoff_ts(kickoff)}. Nobody else can see it until then.",
            ephemeral=True,
        )

    @score.autocomplete("match")
    async def match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        term = current.strip().lower()
        choices = []
        for row in self._open_matches():
            label = f"{row['home']} vs {row['away']}"
            if term and term not in label.lower():
                continue
            kickoff = parse_utc(row["kickoff_utc"]).astimezone(self.cfg.tz)
            name = f"MD{row['matchday']} · {label} · {kickoff.strftime('%a %d %b %H:%M')}"
            choices.append(
                app_commands.Choice(name=truncate(name, 100), value=str(row["match_id"]))
            )
        return choices[:25]

    @predict.command(name="fixtures", description="This matchweek, with your picks.")
    @app_commands.describe(matchday="Which matchweek (defaults to the current one)")
    async def fixtures(
        self,
        interaction: discord.Interaction,
        matchday: app_commands.Range[int, 1, 60] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await refresh_competitions(self.api, self.db, [self.competition], days_ahead=14)
        except FootballAPIError as exc:
            log.warning("refresh failed: %s", exc)

        md = int(matchday) if matchday else self._current_matchday()
        if md is None:
            await interaction.followup.send(
                f"No {self.competition_name} fixtures mirrored yet — try again in a minute.",
                ephemeral=True,
            )
            return
        rows = self._matchday_rows(md)
        if not rows:
            await interaction.followup.send(f"Nothing for matchweek {md}.", ephemeral=True)
            return

        mine = self._my_predictions(interaction.user.id, [r["match_id"] for r in rows])
        counts = self._prediction_counts([r["match_id"] for r in rows])
        lines: list[str] = []
        current_day = None
        for row in rows:
            kickoff = parse_utc(row["kickoff_utc"])
            day = local_day(kickoff, self.cfg.tz)
            if day != current_day:
                lines.append(f"\n**{day}**")
                current_day = day
            pick = mine.get(row["match_id"])
            fixture = f"{row['home']} vs {row['away']}"
            if row["status"] in FINISHED_STATUSES:
                actual = fmt_score(row["home_goals"], row["away_goals"])
                got = (
                    score_prediction(pick[0], pick[1], row["home_goals"], row["away_goals"])
                    if pick
                    else None
                )
                tail = (
                    f"you said {pick[0]}–{pick[1]} → **{got} pt**"
                    if pick
                    else "_you didn't predict_"
                )
                lines.append(f"✅ {row['home']} **{actual}** {row['away']} · {tail}")
            elif row["status"] in DEAD_STATUSES:
                lines.append(f"🚫 {fixture} · {row['status'].lower()}")
            else:
                yours = f"**{pick[0]}–{pick[1]}**" if pick else "—"
                n = counts.get(row["match_id"], 0)
                lines.append(
                    f"{kickoff_ts(kickoff, 't')} {fixture} · you: {yours} · {n} in"
                )

        e = embed(
            f"🎯 {self.competition_name} matchweek {md}",
            colour=TABLE_COLOUR,
            description=(
                f"`/predict score` to submit. {POINTS_EXACT} pts exact, "
                f"{POINTS_OUTCOME} pt result."
            ),
        )
        for block in chunk_lines([line for line in lines if line]):
            e.add_field(name="​", value=block, inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @predict.command(name="mine", description="Your picks, and what you still owe.")
    @app_commands.describe(matchday="Which matchweek (defaults to the current one)")
    async def mine(
        self,
        interaction: discord.Interaction,
        matchday: app_commands.Range[int, 1, 60] | None = None,
    ) -> None:
        md = int(matchday) if matchday else self._current_matchday()
        if md is None:
            await interaction.response.send_message(
                "No fixtures mirrored yet.", ephemeral=True
            )
            return
        rows = self._matchday_rows(md)
        mine = self._my_predictions(interaction.user.id, [r["match_id"] for r in rows])
        open_rows = [r for r in rows if r["status"] in UNPLAYED_STATUSES]
        missing = [r for r in open_rows if r["match_id"] not in mine]
        done = [r for r in rows if r["match_id"] in mine]

        e = embed(f"🎯 Your matchweek {md}", colour=TABLE_COLOUR)
        if done:
            e.add_field(
                name=f"Submitted ({len(done)})",
                value="\n".join(
                    f"{r['home']} **{mine[r['match_id']][0]}–{mine[r['match_id']][1]}** {r['away']}"
                    for r in done
                )[:1000],
                inline=False,
            )
        if missing:
            e.add_field(
                name=f"Still open ({len(missing)})",
                value="\n".join(
                    f"{r['home']} vs {r['away']} — {kickoff_ts(parse_utc(r['kickoff_utc']), 'R')}"
                    for r in missing
                )[:1000],
                inline=False,
            )
        if not done and not missing:
            e.description = "Nothing to predict in this matchweek."
        total = self.db.query_one(
            """SELECT COALESCE(SUM(s.points), 0) AS pts FROM prediction_scores s
               JOIN matches m ON m.match_id = s.match_id
               WHERE s.user_id=? AND m.competition=? AND m.matchday=?""",
            (interaction.user.id, self.competition, md),
        )
        e.set_footer(text=f"Matchweek {md} so far: {total['pts'] if total else 0} pts")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @predict.command(name="table", description="The leaderboard.")
    @app_commands.describe(matchday="Restrict to one matchweek (default: whole season)")
    async def table(
        self,
        interaction: discord.Interaction,
        matchday: app_commands.Range[int, 1, 60] | None = None,
    ) -> None:
        e = self._leaderboard_embed(int(matchday) if matchday else None)
        if e is None:
            await interaction.response.send_message(
                "Nothing scored yet — the table appears once a matchweek has been played.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=e)

    # -- leaderboard -------------------------------------------------------

    def _leaderboard_embed(self, matchday: int | None) -> discord.Embed | None:
        params: list = [self.competition]
        clause = ""
        if matchday is not None:
            clause = " AND m.matchday=?"
            params.append(matchday)
        rows = self.db.query(
            f"""SELECT s.user_id,
                       SUM(s.points) AS pts,
                       COUNT(*) AS played,
                       SUM(CASE WHEN s.points=? THEN 1 ELSE 0 END) AS exact
                FROM prediction_scores s JOIN matches m ON m.match_id = s.match_id
                WHERE m.competition=?{clause}
                GROUP BY s.user_id
                ORDER BY pts DESC, exact DESC, played ASC""",
            [POINTS_EXACT, *params],
        )
        if not rows:
            return None
        title = (
            f"🏆 {self.competition_name} predictions — matchweek {matchday}"
            if matchday is not None
            else f"🏆 {self.competition_name} predictions — season table"
        )
        lines = [
            f"{medal(i)} <@{r['user_id']}> — **{r['pts']}** pts "
            f"_({r['exact']} spot on, {r['played']} matches)_"
            for i, r in enumerate(rows, start=1)
        ]
        return embed(title, colour=TABLE_COLOUR, description="\n".join(lines))

    # -- background loop ---------------------------------------------------

    @tasks.loop(minutes=SCORE_LOOP_MINUTES)
    async def score_loop(self) -> None:
        try:
            await refresh_competitions(
                self.api, self.db, [self.competition], days_back=5, days_ahead=14
            )
        except FootballAPIError as exc:
            log.warning("prediction refresh failed: %s", exc)

        self._score_finished()
        await self._announce_new_matchweek()
        await self._post_wrapups()

    @score_loop.before_loop
    async def before_score_loop(self) -> None:
        await self.bot.wait_until_ready()

    def _score_finished(self) -> int:
        """Award points for any played match that isn't scored yet. Idempotent."""
        rows = self.db.query(
            f"""SELECT p.match_id, p.user_id, p.home_goals AS ph, p.away_goals AS pa,
                       m.home_goals AS ah, m.away_goals AS aa
                FROM predictions p
                JOIN matches m ON m.match_id = p.match_id
                LEFT JOIN prediction_scores s
                       ON s.match_id = p.match_id AND s.user_id = p.user_id
                WHERE s.match_id IS NULL
                  AND m.status IN {sql_str_tuple(FINISHED_STATUSES)}
                  AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL""",
        )
        if not rows:
            return 0
        now = utcnow_iso()
        self.db.executemany(
            """INSERT INTO prediction_scores (match_id, user_id, points, scored_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(match_id, user_id) DO NOTHING""",
            [
                (
                    r["match_id"],
                    r["user_id"],
                    score_prediction(r["ph"], r["pa"], r["ah"], r["aa"]),
                    now,
                )
                for r in rows
            ],
        )
        log.info("scored %d prediction(s)", len(rows))
        return len(rows)

    async def _channels(self) -> list[discord.TextChannel]:
        out = []
        for guild_id, channel_id in self.db.configured_guilds("predictions"):
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if isinstance(channel, discord.TextChannel):
                out.append(channel)
        return out

    def _matchday_in_view(self) -> tuple[int, str] | None:
        """(matchday, first kickoff) for the next matchweek worth announcing."""
        now = utcnow()
        horizon = (now + timedelta(days=ANNOUNCE_WITHIN_DAYS)).isoformat(timespec="seconds")
        row = self.db.query_one(
            f"""SELECT matchday, MIN(kickoff_utc) AS first_kickoff FROM matches
                WHERE competition=? AND matchday IS NOT NULL
                  AND status IN {sql_str_tuple(UNPLAYED_STATUSES)}
                  AND kickoff_utc BETWEEN ? AND ?
                GROUP BY matchday ORDER BY first_kickoff LIMIT 1""",
            (self.competition, now.isoformat(timespec="seconds"), horizon),
        )
        if row is None or row["matchday"] is None:
            return None
        return row["matchday"], row["first_kickoff"]

    async def _announce_new_matchweek(self) -> None:
        """Post the fixture list once, when a matchweek comes into view."""
        found = self._matchday_in_view()
        if found is None:
            return
        md, first_kickoff = found
        channels = await self._channels()
        if not channels:
            return
        if not self.db.claim_announcement(f"mw:{self.competition}:{md}"):
            return

        rows = self._matchday_rows(md)
        lines: list[str] = []
        current_day = None
        for match in rows:
            kickoff = parse_utc(match["kickoff_utc"])
            day = local_day(kickoff, self.cfg.tz)
            if day != current_day:
                lines.append(f"\n**{day}**")
                current_day = day
            lines.append(f"{kickoff_ts(kickoff, 't')} {match['home']} vs {match['away']}")
        first = parse_utc(first_kickoff)
        e = embed(
            f"🎯 Matchweek {md} is open",
            colour=TABLE_COLOUR,
            description=(
                f"`/predict score` for each fixture. {POINTS_EXACT} pts for the exact score, "
                f"{POINTS_OUTCOME} pt for the right result.\n"
                f"Each fixture locks at its own kickoff — first one is {kickoff_ts(first)}."
            ),
        )
        for block in chunk_lines([line for line in lines if line]):
            e.add_field(name="​", value=block, inline=False)
        for channel in channels:
            try:
                await channel.send(embed=e)
            except discord.HTTPException:
                log.exception("failed to announce matchweek %s", md)

    def _completed_matchdays(self) -> list[int]:
        """Matchweeks with nothing left to play, at least one result, and players.

        A postponed fixture counts as neither played nor pending, so one call-off
        does not hold the whole matchweek's wrap-up hostage.
        """
        played = sql_str_tuple(FINISHED_STATUSES)
        unplayed = sql_str_tuple(UNPLAYED_STATUSES)
        rows = self.db.query(
            f"""SELECT m.matchday AS md,
                       SUM(CASE WHEN m.status IN {played} THEN 1 ELSE 0 END) AS finished,
                       SUM(CASE WHEN m.status IN {unplayed} THEN 1 ELSE 0 END) AS pending,
                       COUNT(DISTINCT p.user_id) AS players
                FROM matches m LEFT JOIN predictions p ON p.match_id = m.match_id
                WHERE m.competition=? AND m.matchday IS NOT NULL
                GROUP BY m.matchday
                HAVING pending = 0 AND finished > 0 AND players > 0
                ORDER BY m.matchday""",
            (self.competition,),
        )
        return [r["md"] for r in rows]

    async def _post_wrapups(self) -> None:
        """Reveal predictions and post the table once a matchweek is done."""
        candidates = self._completed_matchdays()
        if not candidates:
            return
        channels = await self._channels()
        if not channels:
            return
        for md in candidates:
            if not self.db.claim_announcement(f"wrap:{self.competition}:{md}"):
                continue
            e = self._wrapup_embed(md)
            table = self._leaderboard_embed(None)
            for channel in channels:
                try:
                    await channel.send(embed=e)
                    if table is not None:
                        await channel.send(embed=table)
                except discord.HTTPException:
                    log.exception("failed to post wrap-up for matchweek %s", md)

    def _wrapup_embed(self, matchday: int) -> discord.Embed:
        rows = self._matchday_rows(matchday)
        lines: list[str] = []
        for match in rows:
            if match["status"] not in FINISHED_STATUSES:
                lines.append(f"🚫 {match['home']} vs {match['away']} — {match['status'].lower()}")
                continue
            actual = fmt_score(match["home_goals"], match["away_goals"])
            lines.append(f"**{match['home']} {actual} {match['away']}**")
            picks = self.db.query(
                """SELECT p.user_id, p.home_goals, p.away_goals, s.points
                   FROM predictions p
                   LEFT JOIN prediction_scores s
                          ON s.match_id = p.match_id AND s.user_id = p.user_id
                   WHERE p.match_id=? ORDER BY s.points DESC""",
                (match["match_id"],),
            )
            if picks:
                lines.append(
                    " · ".join(
                        f"<@{p['user_id']}> {p['home_goals']}–{p['away_goals']}"
                        f"{' 🎯' if p['points'] == POINTS_EXACT else ''}"
                        for p in picks
                    )
                )
        best = self.db.query_one(
            """SELECT s.user_id, SUM(s.points) AS pts FROM prediction_scores s
               JOIN matches m ON m.match_id = s.match_id
               WHERE m.competition=? AND m.matchday=?
               GROUP BY s.user_id ORDER BY pts DESC LIMIT 1""",
            (self.competition, matchday),
        )
        description = (
            f"Matchweek winner: <@{best['user_id']}> with **{best['pts']}** pts."
            if best
            else None
        )
        e = embed(
            f"📊 Matchweek {matchday} — how it went",
            colour=TABLE_COLOUR,
            description=description,
        )
        for block in chunk_lines(lines):
            e.add_field(name="​", value=block, inline=False)
        return e


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Predictions(bot))
