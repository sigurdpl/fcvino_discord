"""The landing page: what the club is, in numbers and in its last few evenings."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import queries
from ..deps import Db, LoggedIn, page

router = APIRouter()

TOP_WINES = 5
RECENT = 4
UPCOMING = 4


@router.get("/")
async def index(request: Request, db: Db, _: LoggedIn):
    trips = queries.trip_summary(db)
    # Every number here is the one the page it links to would show, because it
    # comes from the same query that page uses. `totals` arrives via `page()`.
    return page(
        request,
        "home.html",
        since=trips["years"][0] if trips["years"] else None,
        trips=trips["trips"],
        grounds=trips["grounds"],
        cities=trips["cities"],
        countries=trips["countries"],
        upcoming=queries.upcoming_events(db, UPCOMING),
        recent=queries.recent_activity(db, RECENT),
        top=queries.top_wines(db, TOP_WINES),
    )
