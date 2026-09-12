"""The cellar: search, one bottle in full, the boards, and the tastings."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from bot import wine_stats

from .. import queries
from ..deps import Db, LoggedIn, WineIndex, page
from ..search import Filters

router = APIRouter(prefix="/wine")

BOARD_TITLES = {"country": "By country", "region": "By region", "grape": "By grape"}


def _filters(
    country: str | None,
    region: str | None,
    grape: str | None,
    member: str | None,
    year_from: int | None,
    year_to: int | None,
    vintage_from: int | None,
    vintage_to: int | None,
    min_score: float | None,
) -> Filters:
    """Blank dropdown values arrive as "", which must mean "no filter"."""
    return Filters(
        country=country or None,
        region=region or None,
        grape=grape or None,
        member=member or None,
        year_from=year_from,
        year_to=year_to,
        vintage_from=vintage_from,
        vintage_to=vintage_to,
        min_score=min_score,
    )


@router.get("")
async def index(
    request: Request,
    db: Db,
    wines: WineIndex,
    _: LoggedIn,
    q: str = "",
    country: str = "",
    region: str = "",
    grape: str = "",
    member: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    vintage_from: int | None = None,
    vintage_to: int | None = None,
    min_score: float | None = None,
    limit: int = Query(60, ge=1, le=500),
):
    filters = _filters(country, region, grape, member, year_from, year_to,
                       vintage_from, vintage_to, min_score)
    hits = wines.search(q, filters, limit=None)
    return page(
        request,
        "wine/index.html",
        totals=queries.cellar_totals(db),
        hits=hits[:limit],
        total_hits=len(hits),
        limit=limit,
        q=q,
        filters=filters,
        countries=wines.facets("country"),
        regions=wines.facets("region"),
        grapes=wines.facets("grape"),
        brought=wines.facets("brought_by"),
    )


@router.get("/results")
async def results(
    request: Request,
    wines: WineIndex,
    _: LoggedIn,
    q: str = "",
    country: str = "",
    region: str = "",
    grape: str = "",
    member: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    vintage_from: int | None = None,
    vintage_to: int | None = None,
    min_score: float | None = None,
    limit: int = Query(60, ge=1, le=500),
):
    """The results table on its own, for HTMX to swap in as you type."""
    filters = _filters(country, region, grape, member, year_from, year_to,
                       vintage_from, vintage_to, min_score)
    hits = wines.search(q, filters, limit=None)
    return page(
        request,
        "wine/_results.html",
        hits=hits[:limit],
        total_hits=len(hits),
        limit=limit,
        q=q,
    )


@router.get("/boards")
async def boards(request: Request, db: Db, _: LoggedIn):
    tables = {}
    for field, title in BOARD_TITLES.items():
        tables[field] = {
            "title": title,
            "rows": wine_stats.tally(queries.board_rows(db, field)),
        }
    placed = wine_stats.coverage(
        db.query("SELECT country, region, grape, vintage FROM wines"),
        ["country", "region", "grape", "vintage"],
    )
    return page(
        request,
        "wine/boards.html",
        tables=tables,
        placed=placed,
        minimum=wine_stats.MIN_WINES,
        totals=queries.cellar_totals(db),
    )


@router.get("/tastings")
async def tastings(request: Request, db: Db, _: LoggedIn):
    return page(request, "wine/tastings.html", tastings=queries.tastings(db))


@router.get("/tastings/{tasting_id}")
async def tasting(request: Request, db: Db, _: LoggedIn, tasting_id: int):
    row = queries.tasting(db, tasting_id)
    if row is None:
        raise HTTPException(404, "No such tasting")
    return page(
        request,
        "wine/tasting.html",
        tasting=row,
        wines=queries.tasting_wines(db, tasting_id),
    )


@router.get("/{wine_id}")
async def detail(request: Request, db: Db, _: LoggedIn, wine_id: int):
    row = queries.wine(db, wine_id)
    if row is None:
        raise HTTPException(404, "No such wine")
    return page(
        request,
        "wine/detail.html",
        wine=row,
        summary=queries.wine_summary(db, wine_id),
        ratings=queries.wine_ratings(db, wine_id),
        elsewhere=queries.same_wine_elsewhere(db, wine_id),
    )
