"""Keeping the local `matches` table in step with football-data.org.

Reminders, predictions and scoring all read the mirror rather than the API, so a
transient upstream failure delays a refresh instead of breaking a command.
"""

from __future__ import annotations

import logging
from datetime import date

from .db import Database
from .football_api import FootballAPI, FootballAPIError, window

log = logging.getLogger(__name__)


async def refresh_competitions(
    api: FootballAPI,
    db: Database,
    codes: list[str] | tuple[str, ...],
    *,
    days_back: int = 3,
    days_ahead: int = 10,
    today: date | None = None,
) -> int:
    """Mirror every match for these competitions in the window. Returns rows written."""
    date_from, date_to = window(days_back, days_ahead, today=today)
    written = 0
    for code in codes:
        try:
            matches = await api.competition_matches(code, date_from=date_from, date_to=date_to)
        except FootballAPIError as exc:
            log.warning("refresh %s failed: %s", code, exc)
            continue
        written += db.upsert_matches(matches)
    return written


async def refresh_favourite_teams(
    api: FootballAPI,
    db: Database,
    *,
    days_back: int = 3,
    days_ahead: int = 10,
    today: date | None = None,
) -> int:
    """Mirror matches for every club a member registered, whatever competition."""
    team_ids = list(db.favourite_teams())
    if not team_ids:
        return 0
    date_from, date_to = window(days_back, days_ahead, today=today)
    written = 0
    for team_id in team_ids:
        try:
            matches = await api.team_matches(team_id, date_from=date_from, date_to=date_to)
        except FootballAPIError as exc:
            log.warning("refresh team %s failed: %s", team_id, exc)
            continue
        written += db.upsert_matches(matches)
    return written
