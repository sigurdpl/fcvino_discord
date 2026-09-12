"""The away-trip archive: one trip a year since 2010, and the matches we saw."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from bot import trip_stats

from .. import queries
from ..deps import Db, LoggedIn, page

router = APIRouter(prefix="/trips")


def _summary(db: Db) -> dict:
    """The headline numbers, all from `bot/trip_stats.py`."""
    trips = queries.trips(db)
    matches = db.trip_match_rows()
    played = trip_stats.played(matches)
    wins, draws, losses = trip_stats.result_split(matches)
    years = trip_stats.years(trips)
    return {
        "trips": trips,
        "matches": matches,
        "countries": trip_stats.country_counts(trips),
        "clubs": trip_stats.club_counts(matches),
        "goals": trip_stats.total_goals(matches),
        "goals_per_game": trip_stats.goals_per_game(matches),
        "grounds": trip_stats.distinct_stadiums(matches),
        "played": played,
        "result_split": (wins, draws, losses),
        "streak": trip_stats.longest_year_streak(years),
        "biggest_win": trip_stats.biggest_win(matches),
        "highest_scoring": trip_stats.highest_scoring(matches),
    }


@router.get("")
async def index(request: Request, db: Db, _: LoggedIn):
    summary = _summary(db)
    by_trip: dict[int, list] = {}
    for match in summary["matches"]:
        by_trip.setdefault(match["trip_id"], []).append(match)
    return page(request, "trips/index.html", matches_by_trip=by_trip, **summary)


@router.get("/{year}")
async def detail(request: Request, db: Db, _: LoggedIn, year: int):
    trip = queries.trip(db, year)
    if trip is None:
        raise HTTPException(404, f"No trip recorded for {year}")
    return page(
        request,
        "trips/detail.html",
        trip=trip,
        matches=db.trip_match_rows(trip_id=trip["id"]),
    )
