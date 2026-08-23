"""The away-trips archive: one place a year since 2010, and every match we saw.

A trip is identified by its year — we go one place a year — and holds as many
matches as we managed while we were there, each with its own city when they
differ.

Hand-entered, deliberately. The football API's free tier has no data before the
2023/24 season and no venue, attendance or goalscorers even inside that window,
so there is nothing to fetch — the interesting detail is typed in or applied by
`scripts/apply_trip_details.py`.

Presentation only. Every statistic comes from `bot/trip_stats.py`, which is pure
and tested on its own.
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
from datetime import date
from typing import NamedTuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import pinned
from .. import trip_stats as stats
from ..formatting import (
    FOOTBALL_COLOUR,
    NEUTRAL_COLOUR,
    TABLE_COLOUR,
    embed,
    fill,
    fmt_score,
    medal,
    truncate,
)

log = logging.getLogger(__name__)

FIRST_TRIP_YEAR = 2010
MAX_LIST_ROWS = 40
TOP_N = 8
# The pinned copy is refreshed on every change, so this is only a safety net.
# An interval rather than a fixed clock time: tasks.loop fires once on startup
# and then every N hours, whereas `time=06:00` would never fire at all on a
# laptop that is closed at six in the morning.
LIST_REFRESH_HOURS = 24
# Discord collapses runs of ordinary spaces in message text, so the hanging
# indent under each year uses em spaces, which survive. Same trap that kept the
# league table from lining up.
LIST_INDENT = "\u2003\u2003"


class ListView(NamedTuple):
    """The archive rendered once, for both `/trips list` and the pinned copy."""

    count: int
    lines: list[str]
    summary: str

    @property
    def fingerprint(self) -> str:
        """Identifies the content, deliberately excluding the footer date.

        If the date were in here, the daily pass would find a new fingerprint
        every day and re-edit a message whose content had not moved.
        """
        return hashlib.sha256("\n".join([*self.lines, self.summary]).encode()).hexdigest()


def parse_day(value: str | None) -> tuple[str | None, str | None]:
    """Validate a YYYY-MM-DD date. Returns (normalised, error message)."""
    if value is None or not value.strip():
        return None, None
    try:
        return date.fromisoformat(value.strip()).isoformat(), None
    except ValueError:
        return None, f"`{truncate(value, 40)}` isn't a date I understand — use `YYYY-MM-DD`."


class Trips(commands.Cog):
    trips = app_commands.Group(name="trips", description="Where FC Vino has been.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.list_loop.start()

    async def cog_unload(self) -> None:
        self.list_loop.cancel()

    # -- the pinned copy ---------------------------------------------------

    @tasks.loop(hours=LIST_REFRESH_HOURS)
    async def list_loop(self) -> None:
        """Safety net. Every change refreshes the pinned copy directly, so this
        mostly finds nothing to do — which costs one hash comparison."""
        await self._refresh_pinned()

    @list_loop.before_loop
    async def before_list_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_pinned(self) -> None:
        """Bring the pinned archive up to date.

        Called after every change as well as daily. Swallows its own failures:
        a channel the bot cannot post in must not make `/trips add` look broken
        when the trip itself was saved perfectly well.
        """
        view = self._list_view()
        if view is None:
            # Nothing recorded. Don't start a pinned message for an empty
            # archive, but do correct one that already exists rather than
            # leaving it showing trips that have since been deleted.
            embed_to_show = embed(
                "✈️ No trips recorded",
                colour=FOOTBALL_COLOUR,
                description="`/trips add` to start the archive again.",
            )
            fingerprint = "empty"
            if not any(
                self.db.get_bot_message(guild_id, "trips")
                for guild_id, _ in self.db.configured_guilds("trips")
            ):
                return
        else:
            embed_to_show = self._list_embed(view, changed_on=date.today())
            fingerprint = view.fingerprint
        try:
            await pinned.publish(
                self.bot,
                "trips",
                embed=embed_to_show,
                fingerprint=fingerprint,
                pin_reason="FC Vino trips archive",
            )
        except Exception:  # noqa: BLE001 - never break the calling command
            log.exception("could not refresh the pinned trips list")

    # -- lookups -----------------------------------------------------------

    def _all_trips(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM trips ORDER BY year, country")

    def _trip_matches(self, trip_id: int) -> list[sqlite3.Row]:
        """A trip's matches in the joined shape, so `city` is already resolved
        against the trip's and the stats helpers can take these rows directly."""
        return self.db.trip_match_rows(trip_id=trip_id)

    def _goals(self, trip_match_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM trip_goals WHERE trip_match_id=? ORDER BY minute, id",
            (trip_match_id,),
        )

    def _trip_for_year(self, year: int) -> sqlite3.Row | None:
        """The trip for a year. One place a year, so the year identifies it."""
        return self.db.trip_by_year(int(year))

    def _resolve_match(self, value: str) -> sqlite3.Row | None:
        value = (value or "").strip()
        if value.isdigit():
            row = self.db.query_one("SELECT * FROM trip_matches WHERE id=?", (int(value),))
            if row is not None:
                return row
        return self.db.query_one(
            "SELECT * FROM trip_matches WHERE home LIKE ? OR away LIKE ? ORDER BY id DESC",
            (f"%{value}%", f"%{value}%"),
        )

    # -- autocompletes -----------------------------------------------------

    async def country_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest countries already in the archive, so spellings stay consistent."""
        rows = self.db.query("SELECT DISTINCT country FROM trips ORDER BY country")
        term = current.strip().lower()
        return [
            app_commands.Choice(name=truncate(r["country"], 100), value=r["country"])
            for r in rows
            if not term or term in r["country"].lower()
        ][:25]

    async def year_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Recorded years, newest first. The option is still a plain integer, so
        a year that has no trip yet can simply be typed."""
        term = current.strip().lower()
        choices = []
        for trip in reversed(self._all_trips()):
            label = f"{trip['year']} · {trip['country']}"
            if trip["city"]:
                label += f" ({trip['city']})"
            if term and term not in label.lower():
                continue
            choices.append(app_commands.Choice(name=truncate(label, 100), value=trip["year"]))
        return choices[:25]

    async def match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Matches, newest first, labelled well enough to tell two games on the
        same trip apart — which is why the date and city are in there."""
        term = current.strip().lower()
        choices = []
        for row in reversed(self.db.trip_match_rows()):
            score = fmt_score(row["home_goals"], row["away_goals"])
            label = f"{row['year']} · {row['home']} {score} {row['away']}"
            extra = [bit for bit in (row["match_date"], row["city"]) if bit]
            if extra:
                label += f" ({' · '.join(extra)})"
            if term and term not in label.lower():
                continue
            choices.append(app_commands.Choice(name=truncate(label, 100), value=str(row["id"])))
        return choices[:25]

    # -- writing -----------------------------------------------------------

    @trips.command(name="add", description="Record a trip (or amend one).")
    @app_commands.describe(
        year="Which year we travelled",
        country="Country we visited",
        city="City",
        date_from="First day, YYYY-MM-DD",
        date_to="Last day, YYYY-MM-DD",
        notes="Anything worth remembering",
    )
    @app_commands.autocomplete(country=country_autocomplete, year=year_autocomplete)
    async def add(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, FIRST_TRIP_YEAR, 2100],
        country: str,
        city: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        notes: str | None = None,
    ) -> None:
        start, start_error = parse_day(date_from)
        end, end_error = parse_day(date_to)
        if start_error or end_error:
            await interaction.response.send_message(start_error or end_error, ephemeral=True)
            return
        if start and end and end < start:
            await interaction.response.send_message(
                "The last day is before the first day.", ephemeral=True
            )
            return

        clean_country = stats.normalise_country(country)
        existing = self._trip_for_year(int(year))
        trip_id = self.db.upsert_trip(
            year=int(year),
            country=clean_country,
            city=(city or "").strip() or None,
            date_from=start,
            date_to=end,
            notes=(notes or "").strip() or None,
            added_by=interaction.user.id,
        )
        trip = self.db.query_one("SELECT * FROM trips WHERE id=?", (trip_id,))
        verb = "Updated" if existing else "Recorded"
        e = self._trip_embed(trip)
        e.set_footer(text=f"Trip #{trip_id} · add the match with /trips add-match")
        await interaction.response.send_message(f"✈️ {verb} the {trip['year']} trip.", embed=e)
        await self._refresh_pinned()

    @trips.command(name="add-match", description="Add a match we saw on a trip.")
    @app_commands.describe(
        year="Which year's trip",
        home="Home team",
        away="Away team",
        home_goals="Home goals",
        away_goals="Away goals",
        match_date="Day of the match, YYYY-MM-DD",
        competition="League, cup, friendly…",
        city="City, if not the same as the rest of the trip",
        stadium="Ground",
        attendance="Crowd, if you know it",
        notes="Anything else",
    )
    @app_commands.autocomplete(year=year_autocomplete)
    async def add_match(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, FIRST_TRIP_YEAR, 2100],
        home: str,
        away: str,
        home_goals: app_commands.Range[int, 0, 30] | None = None,
        away_goals: app_commands.Range[int, 0, 30] | None = None,
        match_date: str | None = None,
        competition: str | None = None,
        city: str | None = None,
        stadium: str | None = None,
        attendance: app_commands.Range[int, 0, 200_000] | None = None,
        notes: str | None = None,
    ) -> None:
        row = self._trip_for_year(year)
        if row is None:
            await interaction.response.send_message(
                f"No {int(year)} trip yet — add it with `/trips add` first.", ephemeral=True
            )
            return
        day, day_error = parse_day(match_date)
        if day_error:
            await interaction.response.send_message(day_error, ephemeral=True)
            return

        match_id = self.db.execute(
            """INSERT INTO trip_matches (trip_id, match_date, competition, home, away,
                                         home_goals, away_goals, city, stadium,
                                         attendance, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                day,
                (competition or "").strip() or None,
                home.strip(),
                away.strip(),
                home_goals if home_goals is None else int(home_goals),
                away_goals if away_goals is None else int(away_goals),
                (city or "").strip() or None,
                (stadium or "").strip() or None,
                attendance if attendance is None else int(attendance),
                (notes or "").strip() or None,
            ),
        )
        match = next(m for m in self.db.trip_match_rows(trip_id=row["id"]) if m["id"] == match_id)
        on_trip = self._trip_matches(row["id"])
        position = next(i for i, m in enumerate(on_trip, start=1) if m["id"] == match_id)
        gaps = stats.missing_detail(match)
        tail = f"\nStill missing: {', '.join(gaps)}." if gaps else ""
        ordinal = f" (match {position} of {len(on_trip)})" if len(on_trip) > 1 else ""
        await interaction.response.send_message(
            f"⚽ Added to the **{row['year']}** trip to {row['country']}{ordinal}: "
            f"{self._fixture_text(match)}{tail}"
        )
        await self._refresh_pinned()

    @trips.command(name="edit-match", description="Correct a match without losing its scorers.")
    @app_commands.describe(
        match="Start typing a team or year",
        home="Home team",
        away="Away team",
        home_goals="Home goals",
        away_goals="Away goals",
        match_date="Day of the match, YYYY-MM-DD",
        competition="League, cup, friendly…",
        city="City the match was in",
        stadium="Ground",
        attendance="Crowd",
        notes="Anything else",
    )
    @app_commands.autocomplete(match=match_autocomplete)
    async def edit_match(
        self,
        interaction: discord.Interaction,
        match: str,
        home: str | None = None,
        away: str | None = None,
        home_goals: app_commands.Range[int, 0, 30] | None = None,
        away_goals: app_commands.Range[int, 0, 30] | None = None,
        match_date: str | None = None,
        competition: str | None = None,
        city: str | None = None,
        stadium: str | None = None,
        attendance: app_commands.Range[int, 0, 200_000] | None = None,
        notes: str | None = None,
    ) -> None:
        """Update the fields you supply on an existing match.

        An UPDATE rather than a delete-and-re-add, which is the whole point:
        `trip_goals` cascades off `trip_matches`, so removing a match to fix one
        digit of the score would take its goalscorers with it.
        """
        row = self._resolve_match(match)
        if row is None:
            await interaction.response.send_message(
                "I don't have that match. Pick one from the suggestions.", ephemeral=True
            )
            return

        day, day_error = parse_day(match_date)
        if day_error:
            await interaction.response.send_message(day_error, ephemeral=True)
            return

        # `is not None` throughout, never truthiness: 0 is a real scoreline and
        # an empty crowd figure is a real number, so neither may read as "the
        # option wasn't supplied".
        changes: dict[str, object] = {}
        for column, value in (
            ("home", (home or "").strip() or None),
            ("away", (away or "").strip() or None),
            ("competition", (competition or "").strip() or None),
            ("city", (city or "").strip() or None),
            ("stadium", (stadium or "").strip() or None),
            ("notes", (notes or "").strip() or None),
            ("match_date", day),
        ):
            if value is not None:
                changes[column] = value
        for column, number in (
            ("home_goals", home_goals),
            ("away_goals", away_goals),
            ("attendance", attendance),
        ):
            if number is not None:
                changes[column] = int(number)

        if not changes:
            await interaction.response.send_message(
                "Tell me what to change — every field is optional, but I need at least one.",
                ephemeral=True,
            )
            return

        assignments = ", ".join(f"{column}=?" for column in changes)
        self.db.execute(
            f"UPDATE trip_matches SET {assignments} WHERE id=?",
            (*changes.values(), row["id"]),
        )
        updated = self.db.query_one("SELECT * FROM trip_matches WHERE id=?", (row["id"],))
        scorers = len(self._goals(row["id"]))
        gaps = stats.missing_detail(
            next(m for m in self.db.trip_match_rows() if m["id"] == row["id"])
        )

        tail = f"\nStill missing: {', '.join(gaps)}." if gaps else ""
        kept = f" {scorers} scorer(s) kept." if scorers else ""
        await interaction.response.send_message(
            f"✏️ Updated {', '.join(sorted(changes))} — "
            f"{self._fixture_text(updated)}.{kept}{tail}"
        )
        await self._refresh_pinned()

    @trips.command(name="add-goals", description="Record who scored in a match we saw.")
    @app_commands.describe(
        match="Start typing a team or year",
        goals="e.g. 23 Haaland H, 67 Foden H, 81 Kane A",
        replace="Replace the existing scorers instead of adding to them",
    )
    @app_commands.autocomplete(match=match_autocomplete)
    async def add_goals(
        self,
        interaction: discord.Interaction,
        match: str,
        goals: str,
        replace: bool = False,
    ) -> None:
        row = self._resolve_match(match)
        if row is None:
            await interaction.response.send_message(
                "I don't have that match. Add it with `/trips add-match` first.", ephemeral=True
            )
            return
        parsed, rejects = stats.parse_goals(goals)
        if not parsed:
            await interaction.response.send_message(
                f"Couldn't read any goals out of that. {stats.GOALS_HELP}", ephemeral=True
            )
            return

        if replace:
            self.db.execute("DELETE FROM trip_goals WHERE trip_match_id=?", (row["id"],))
        self.db.executemany(
            "INSERT INTO trip_goals (trip_match_id, minute, scorer, side) VALUES (?, ?, ?, ?)",
            [(row["id"], g.minute, g.scorer, g.side) for g in parsed],
        )
        message = f"⚽ Recorded {len(parsed)} goal(s) for {self._fixture_text(row)}."
        if rejects:
            message += (
                f"\n⚠️ Skipped {len(rejects)}: `{truncate(', '.join(rejects), 200)}`. "
                f"{stats.GOALS_HELP}"
            )
        await interaction.response.send_message(message)
        await self._refresh_pinned()

    @trips.command(name="remove", description="Delete a trip and every match on it.")
    @app_commands.describe(year="Which year's trip")
    @app_commands.autocomplete(year=year_autocomplete)
    async def remove(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, FIRST_TRIP_YEAR, 2100],
    ) -> None:
        row = self._trip_for_year(year)
        if row is None:
            await interaction.response.send_message(
                f"No {int(year)} trip recorded.", ephemeral=True
            )
            return
        if not self._may_delete(interaction, row["added_by"]):
            await interaction.response.send_message(
                f"<@{row['added_by']}> recorded that trip — ask them, or an admin, to remove it.",
                ephemeral=True,
            )
            return
        count = len(self._trip_matches(row["id"]))
        self.db.execute("DELETE FROM trips WHERE id=?", (row["id"],))
        matches = f", {count} match(es) and all" if count else " and"
        await interaction.response.send_message(
            f"🗑️ Removed the {row['year']} trip to {row['country']}{matches} its goals."
        )
        await self._refresh_pinned()

    @trips.command(name="remove-match", description="Delete one match, keeping the trip.")
    @app_commands.describe(match="Start typing a team or year")
    @app_commands.autocomplete(match=match_autocomplete)
    async def remove_match(self, interaction: discord.Interaction, match: str) -> None:
        row = self._resolve_match(match)
        if row is None:
            await interaction.response.send_message("No such match.", ephemeral=True)
            return
        trip = self.db.query_one("SELECT * FROM trips WHERE id=?", (row["trip_id"],))
        if trip is not None and not self._may_delete(interaction, trip["added_by"]):
            await interaction.response.send_message(
                f"<@{trip['added_by']}> recorded that trip — ask them, or an admin.",
                ephemeral=True,
            )
            return
        self.db.execute("DELETE FROM trip_matches WHERE id=?", (row["id"],))
        left = len(self._trip_matches(row["trip_id"]))
        tail = (
            f" {left} match(es) still on that trip."
            if left
            else " That trip has no matches now."
        )
        await interaction.response.send_message(
            f"🗑️ Removed {self._fixture_text(row)}.{tail}"
        )
        await self._refresh_pinned()

    def _may_delete(self, interaction: discord.Interaction, added_by: int) -> bool:
        """Your own entries, or anything if you can manage the server."""
        if added_by == interaction.user.id:
            return True
        return (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )

    # -- reading -----------------------------------------------------------

    # -- the archive as one view, shared by the command and the pinned copy --

    def _list_view(self) -> ListView | None:
        """The whole archive rendered, or None while nothing is recorded."""
        trips = self._all_trips()
        if not trips:
            return None

        lines: list[str] = []
        for trip in trips[:MAX_LIST_ROWS]:
            matches = self._trip_matches(trip["id"])
            where = trip["country"] + (f", {trip['city']}" if trip["city"] else "")
            lines.append(f"**{trip['year']}**{LIST_INDENT}{where}")
            if not matches:
                lines.append(f"{LIST_INDENT}_no match recorded_")
                continue
            fixtures = " · ".join(self._fixture_text(m) for m in matches)
            count = f" _({len(matches)} matches)_" if len(matches) > 1 else ""
            lines.append(f"{LIST_INDENT}{fixtures}{count}")

        all_matches = self.db.trip_match_rows()
        bits = [
            f"**{len(stats.country_counts(trips))}** countries",
            f"**{len(all_matches)}** matches",
            f"**{stats.total_goals(all_matches)}** goals",
        ]
        grounds = stats.distinct_stadiums(all_matches)
        if grounds:
            bits.append(f"**{len(grounds)}** grounds")
        return ListView(count=len(trips), lines=lines, summary=" · ".join(bits))

    def _list_embed(self, view: ListView, *, changed_on: date | None = None) -> discord.Embed:
        e = embed(f"✈️ {view.count} trips", colour=FOOTBALL_COLOUR)
        fill(e, [*view.lines, "", view.summary])
        if changed_on is not None:
            e.set_footer(text=f"Last changed {changed_on.strftime('%-d %B %Y')}")
        return e

    @trips.command(name="list", description="Every trip, in order.")
    async def list_trips(self, interaction: discord.Interaction) -> None:
        view = self._list_view()
        if view is None:
            await interaction.response.send_message(
                "Nothing recorded yet. `/trips add` to start with 2010.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self._list_embed(view))

    @trips.command(name="show", description="One trip in full.")
    @app_commands.describe(year="Which year's trip")
    @app_commands.autocomplete(year=year_autocomplete)
    async def show(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, FIRST_TRIP_YEAR, 2100],
    ) -> None:
        row = self._trip_for_year(year)
        if row is None:
            await interaction.response.send_message(
                f"No {int(year)} trip recorded.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self._trip_embed(row, full=True))

    @trips.command(name="countries", description="Countries we've been to.")
    async def countries(self, interaction: discord.Interaction) -> None:
        trips = self._all_trips()
        counts = stats.country_counts(trips)
        if not counts:
            await interaction.response.send_message("No trips recorded yet.", ephemeral=True)
            return
        by_country: dict[str, list[int]] = {}
        for trip in trips:
            by_country.setdefault(trip["country"], []).append(trip["year"])
        lines = [
            f"{medal(i)} **{country}** — {n}× "
            f"({', '.join(str(y) for y in sorted(by_country[country]))})"
            for i, (country, n) in enumerate(counts, start=1)
        ]
        e = embed(
            f"🌍 {len(counts)} countries",
            colour=FOOTBALL_COLOUR,
            description="\n".join(lines)[:4000],
        )
        await interaction.response.send_message(embed=e)

    @trips.command(name="teams", description="Clubs we've watched.")
    async def teams(self, interaction: discord.Interaction) -> None:
        matches = self.db.trip_match_rows()
        counts = stats.club_counts(matches)
        if not counts:
            await interaction.response.send_message("No matches recorded yet.", ephemeral=True)
            return
        repeats = stats.repeat_clubs(matches)
        e = embed(f"🏟️ {len(counts)} clubs seen", colour=FOOTBALL_COLOUR)
        e.add_field(
            name="Seen more than once" if repeats else "Nobody twice yet",
            value="\n".join(f"**{club}** — {n}×" for club, n in repeats[:TOP_N])
            or "Every club exactly once.",
            inline=False,
        )
        e.add_field(
            name="All clubs",
            value=truncate(", ".join(club for club, _ in counts), 1000),
            inline=False,
        )
        competitions = stats.competition_counts(matches)
        if competitions:
            e.add_field(
                name="Competitions",
                value="\n".join(f"{name} — {n}×" for name, n in competitions[:TOP_N]),
                inline=False,
            )
        await interaction.response.send_message(embed=e)

    @trips.command(name="stats", description="The numbers behind the trips.")
    async def trip_statistics(self, interaction: discord.Interaction) -> None:
        trips = self._all_trips()
        matches = self.db.trip_match_rows()
        if not trips:
            await interaction.response.send_message("No trips recorded yet.", ephemeral=True)
            return

        countries = stats.country_counts(trips)
        year_list = stats.years(trips)
        scored = stats.played(matches)
        home, draw, away = stats.result_split(matches)
        e = embed("📊 FC Vino on tour", colour=TABLE_COLOUR)

        grounds = stats.distinct_stadiums(matches)
        cities = stats.distinct_cities(matches)
        crowd = stats.total_attendance(matches)
        e.add_field(
            name="🌍 Countries and grounds",
            value=(
                f"**{len(countries)}** countries · **{len(trips)}** trips · "
                f"**{len(matches)}** matches\n"
                f"**{len(cities)}** cities · **{len(grounds)}** different grounds"
                + (f"\n**{crowd:,}**".replace(",", " ") + " people alongside us" if crowd else "")
            ),
            inline=False,
        )

        if scored:
            per_game = stats.goals_per_game(matches)
            biggest = stats.biggest_win(matches)
            wildest = stats.highest_scoring(matches)
            nils = stats.goalless(matches)
            value = (
                f"**{stats.total_goals(matches)}** goals in {len(scored)} match(es) — "
                f"**{per_game:.2f}** a game\n"
                f"Home **{home}** · Draw **{draw}** · Away **{away}**"
            )
            if biggest is not None:
                value += f"\nBiggest win: {self._fixture_text(biggest)} ({biggest['year']})"
            if wildest is not None:
                value += f"\nMost goals: {self._fixture_text(wildest)} ({wildest['year']})"
            if nils:
                value += f"\n0-0s endured: **{len(nils)}**"
            e.add_field(name="⚽ Goals and results", value=value, inline=False)

        clubs = stats.club_counts(matches)
        if clubs:
            most = clubs[0]
            repeats = stats.repeat_clubs(matches)
            e.add_field(
                name="👕 Teams",
                value=(
                    f"**{len(clubs)}** clubs · most seen: **{most[0]}** ({most[1]}×)\n"
                    f"Seen more than once: **{len(repeats)}**"
                ),
                inline=False,
            )

        streak = stats.longest_year_streak(year_list)
        best_year = stats.goals_by_year(matches)
        superlatives = []
        if streak:
            span = streak[1] - streak[0] + 1
            superlatives.append(f"Longest run: **{span}** straight years ({streak[0]}–{streak[1]})")
        if year_list:
            superlatives.append(f"First trip **{year_list[0]}**, latest **{year_list[-1]}**")
        if best_year:
            superlatives.append(f"Best year for goals: **{best_year[0][0]}** ({best_year[0][1]})")
        gaps = [y for y in range(year_list[0], year_list[-1] + 1) if y not in set(year_list)]
        if gaps:
            superlatives.append(f"Years missing from the archive: {', '.join(map(str, gaps))}")
        e.add_field(name="🏅 Streaks", value="\n".join(superlatives), inline=False)
        await interaction.response.send_message(embed=e)

    @trips.command(name="search", description="Find a trip by team, country, city or ground.")
    @app_commands.describe(query="Anything to match on")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        like = f"%{query.strip()}%"
        rows = self.db.query(
            """SELECT m.*, t.year AS year, t.country AS country, t.city AS trip_city
               FROM trip_matches m JOIN trips t ON t.id = m.trip_id
               WHERE m.home LIKE ? OR m.away LIKE ? OR m.stadium LIKE ?
                     OR m.competition LIKE ? OR t.country LIKE ? OR t.city LIKE ?
               ORDER BY t.year""",
            (like, like, like, like, like, like),
        )
        if not rows:
            await interaction.response.send_message(
                f"Nothing matches **{truncate(query, 60)}**.", ephemeral=True
            )
            return
        lines = [
            f"**{r['year']}** {r['country']} — {self._fixture_text(r)}"
            + (f" · {r['stadium']}" if r["stadium"] else "")
            for r in rows
        ]
        e = embed(
            f"🔍 {len(rows)} match(es) for “{truncate(query, 50)}”",
            colour=FOOTBALL_COLOUR,
            description="\n".join(lines)[:4000],
        )
        await interaction.response.send_message(embed=e)

    @trips.command(name="random", description="Remember a trip at random.")
    async def random_trip(self, interaction: discord.Interaction) -> None:
        trips = self._all_trips()
        if not trips:
            await interaction.response.send_message("No trips recorded yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "🎲 Remember this one?", embed=self._trip_embed(random.choice(trips), full=True)
        )

    @trips.command(name="missing", description="What the archive still needs filling in.")
    async def missing(self, interaction: discord.Interaction) -> None:
        trips = self._all_trips()
        if not trips:
            await interaction.response.send_message("No trips recorded yet.", ephemeral=True)
            return
        lines = []
        for trip in trips:
            matches = self._trip_matches(trip["id"])
            if not matches:
                lines.append(f"**{trip['year']}** {trip['country']} — no match recorded")
                continue
            for match in matches:
                gaps = stats.missing_detail(match)
                if not self._goals(match["id"]):
                    gaps.append("scorers")
                if gaps:
                    lines.append(
                        f"**{trip['year']}** {self._fixture_text(match)} — {', '.join(gaps)}"
                    )
        if not lines:
            await interaction.response.send_message(
                "✅ Nothing missing — every trip has a date, competition, score, "
                "ground, crowd and scorers.",
                ephemeral=True,
            )
            return
        e = embed(
            f"📝 {len(lines)} thing(s) to fill in",
            colour=NEUTRAL_COLOUR,
            description="Add detail with `/trips add-match` or `/trips add-goals`.",
        )
        fill(e, lines)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _fixture_text(match: sqlite3.Row) -> str:
        score = fmt_score(match["home_goals"], match["away_goals"])
        return f"{match['home']} **{score}** {match['away']}"

    def _trip_embed(self, trip: sqlite3.Row, *, full: bool = False) -> discord.Embed:
        where = trip["country"] + (f", {trip['city']}" if trip["city"] else "")
        e = embed(f"✈️ {trip['year']} · {where}", colour=FOOTBALL_COLOUR)
        if trip["date_from"]:
            when = trip["date_from"]
            if trip["date_to"] and trip["date_to"] != trip["date_from"]:
                when += f" → {trip['date_to']}"
            e.add_field(name="Dates", value=when, inline=True)
        if trip["notes"]:
            e.add_field(name="Notes", value=truncate(trip["notes"], 1000), inline=False)

        matches = self._trip_matches(trip["id"])
        if not matches:
            e.add_field(
                name="Match", value="Not recorded yet — `/trips add-match`.", inline=False
            )
            return e

        for match in matches:
            details = []
            if match["competition"]:
                details.append(match["competition"])
            if match["match_date"]:
                details.append(match["match_date"])
            if match["stadium"]:
                details.append(match["stadium"])
            if match["match_city"] and match["match_city"] != trip["city"]:
                details.append(match["match_city"])
            if match["attendance"]:
                details.append(f"{match['attendance']:,}".replace(",", " ") + " in")
            body = " · ".join(details)
            if full:
                goals = self._goals(match["id"])
                if goals:
                    scorers = ", ".join(
                        f"{g['minute']}' {g['scorer']}"
                        + (f" ({g['side']})" if g["side"] else "")
                        for g in goals
                    )
                    body += f"\n⚽ {scorers}"
                if match["notes"]:
                    body += f"\n> {truncate(match['notes'], 300)}"
            e.add_field(name=self._fixture_text(match), value=body or "​", inline=False)
        return e


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trips(bot))
