"""Fixtures, results and the league table.

Fixtures and results come from the local `matches` mirror the bot keeps, so
opening this page costs no API call at all. Only the table is fetched live, and
`FootballAPI` caches it for half an hour — comfortably inside the free tier's
ten requests a minute even if all nine of us reload at once.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from bot.config import FREE_COMPETITIONS
from bot.football_api import FootballAPIError

from .. import queries
from ..deps import Cfg, Db, LoggedIn, page

log = logging.getLogger(__name__)

router = APIRouter(prefix="/football")


def standings_tables(payload: dict) -> list[dict]:
    """The overall tables in a standings payload, group stages included."""
    return [s for s in payload.get("standings", []) if s.get("type") == "TOTAL"]


@router.get("")
async def index(request: Request, db: Db, cfg: Cfg, _: LoggedIn, competition: str = ""):
    code = (competition or cfg.prediction_competition).upper()
    if code not in FREE_COMPETITIONS:
        code = cfg.prediction_competition

    tables: list[dict] = []
    error = None
    api = request.app.state.football
    if api is None:
        error = "No FOOTBALL_DATA_TOKEN set, so the live table is unavailable."
    else:
        try:
            tables = standings_tables(await api.standings(code))
        except FootballAPIError as exc:
            # A flat upstream must not take the whole page down: the mirror
            # still has fixtures and results worth reading.
            log.warning("standings for %s failed: %s", code, exc)
            error = f"Couldn't reach football-data.org just now ({exc})."

    return page(
        request,
        "football/index.html",
        code=code,
        competition_name=FREE_COMPETITIONS.get(code, code),
        competitions=FREE_COMPETITIONS,
        mirrored=queries.competitions(db),
        tables=tables,
        error=error,
        fixtures=queries.fixtures(db),
        results=queries.results(db),
        tz=cfg.tz,
    )
