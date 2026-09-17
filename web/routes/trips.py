"""The away-trip archive: one trip a year since 2010, and the matches we saw."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import queries
from ..deps import Db, LoggedIn, page

router = APIRouter(prefix="/trips")


@router.get("")
async def index(request: Request, db: Db, _: LoggedIn):
    summary = queries.trip_summary(db)
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
