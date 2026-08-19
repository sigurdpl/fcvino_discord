"""football-data.org v4 client.

Everything that touches the football API goes through here, so the free tier's
10 requests/minute is enforced in exactly one place. We aim at 8/minute to keep
headroom, and cache responses because nine friends asking for the same fixture
list is one upstream call, not nine.

Docs: https://docs.football-data.org/general/v4/
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import aiohttp

from .config import FREE_COMPETITIONS
from .db import parse_utc

log = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"

# Cache lifetimes, in seconds.
TTL_UPCOMING = 600  # fixture lists move rarely; 10 min is plenty
TTL_RESULTS = 300  # short, so a finished score shows up promptly
TTL_STANDINGS = 1800
TTL_TEAMS = 7 * 24 * 3600

UNPLAYED_STATUSES = ("SCHEDULED", "TIMED")
FINISHED_STATUSES = ("FINISHED", "AWARDED")
DEAD_STATUSES = ("POSTPONED", "CANCELLED", "SUSPENDED")


class FootballAPIError(RuntimeError):
    """Upstream failure with a message safe to show in Discord."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` in any `period` seconds."""

    def __init__(
        self,
        max_calls: int = 8,
        period: float = 60.0,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
                log.debug("rate limit reached, waiting %.1fs", wait)
                await self._sleep(max(wait, 0.01))


def normalize_match(payload: dict[str, Any], competition: str | None = None) -> dict[str, Any]:
    """Flatten one API match into the shape `Database.upsert_matches` expects.

    Kickoff is re-serialised to a `+00:00` offset so timestamps in the DB sort
    and compare as plain strings.
    """
    home = payload.get("homeTeam") or {}
    away = payload.get("awayTeam") or {}
    score = (payload.get("score") or {}).get("fullTime") or {}
    comp = competition or ((payload.get("competition") or {}).get("code")) or "?"
    return {
        "match_id": payload["id"],
        "competition": comp,
        "matchday": payload.get("matchday"),
        "home_id": home.get("id"),
        "home": home.get("shortName") or home.get("name") or "TBD",
        "away_id": away.get("id"),
        "away": away.get("shortName") or away.get("name") or "TBD",
        "kickoff_utc": parse_utc(payload["utcDate"]).isoformat(timespec="seconds"),
        "status": payload.get("status") or "SCHEDULED",
        "home_goals": score.get("home"),
        "away_goals": score.get("away"),
    }


class FootballAPI:
    def __init__(
        self,
        token: str,
        *,
        limiter: RateLimiter | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._limiter = limiter or RateLimiter()
        self._session = session
        self._owns_session = session is None
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._cache: dict[str, tuple[float, Any]] = {}

    # -- plumbing ----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-Auth-Token": self._token},
                timeout=aiohttp.ClientTimeout(total=20),
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> str:
        return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

    async def _get(self, path: str, params: dict[str, Any] | None = None, ttl: int = TTL_UPCOMING):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = self._cache_key(path, params)
        hit = self._cache.get(key)
        now = self._clock()
        if hit and hit[0] > now:
            return hit[1]

        session = await self._get_session()
        for attempt in (1, 2):
            await self._limiter.acquire()
            try:
                async with session.get(f"{BASE_URL}{path}", params=params) as resp:
                    if resp.status == 429:
                        # The API tells us how long to sit out; obey it once, then give up.
                        body = await resp.json(content_type=None)
                        wait = (
                            float(body.get("seconds_to_wait") or 6)
                            if isinstance(body, dict)
                            else 6.0
                        )
                        log.warning("football API rate limited, waiting %.0fs", wait)
                        if attempt == 1:
                            await self._sleep(min(wait + 1, 60))
                            continue
                        raise FootballAPIError(
                            "football-data.org is rate limiting us. Try again in a minute.",
                            status=429,
                        )
                    if resp.status == 403:
                        raise FootballAPIError(
                            "That competition isn't included in the free football-data.org tier.",
                            status=403,
                        )
                    if resp.status == 400:
                        raise FootballAPIError(
                            "football-data.org rejected that request as invalid.", status=400
                        )
                    if resp.status == 404:
                        raise FootballAPIError(
                            "football-data.org has no such resource.", status=404
                        )
                    if resp.status >= 500:
                        raise FootballAPIError(
                            "football-data.org is having a moment. Try again shortly.",
                            status=resp.status,
                        )
                    if resp.status != 200:
                        raise FootballAPIError(
                            f"football-data.org returned HTTP {resp.status}.", status=resp.status
                        )
                    data = await resp.json()
            except aiohttp.ClientError as exc:
                raise FootballAPIError(f"Could not reach football-data.org: {exc}") from exc
            except TimeoutError as exc:
                raise FootballAPIError("football-data.org timed out.") from exc

            self._cache[key] = (self._clock() + ttl, data)
            return data
        raise FootballAPIError("football-data.org could not be reached.")  # pragma: no cover

    # -- endpoints ---------------------------------------------------------

    async def competition_matches(
        self,
        code: str,
        *,
        date_from: date,
        date_to: date,
        status: Sequence[str] | None = None,
        ttl: int = TTL_UPCOMING,
    ) -> list[dict[str, Any]]:
        """Matches for one competition in a date window (inclusive)."""
        params: dict[str, Any] = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
        }
        if status:
            params["status"] = ",".join(status)
        data = await self._get(f"/competitions/{code}/matches", params, ttl=ttl)
        return [normalize_match(m, code) for m in data.get("matches", [])]

    async def team_matches(
        self,
        team_id: int,
        *,
        date_from: date,
        date_to: date,
        ttl: int = TTL_UPCOMING,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/teams/{team_id}/matches",
            {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
            ttl=ttl,
        )
        return [normalize_match(m) for m in data.get("matches", [])]

    async def standings(self, code: str) -> dict[str, Any]:
        return await self._get(f"/competitions/{code}/standings", ttl=TTL_STANDINGS)

    async def competition_teams(self, code: str) -> list[dict[str, Any]]:
        data = await self._get(f"/competitions/{code}/teams", ttl=TTL_TEAMS)
        return [
            {
                "id": t["id"],
                "name": t.get("shortName") or t.get("name"),
                "full_name": t.get("name"),
                "competition": code,
            }
            for t in data.get("teams", [])
        ]

    async def selectable_teams(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        """Teams a member can register as their club, deduplicated by team id.

        A failing competition is skipped rather than sinking the whole list —
        some cups have no squad list until the draw is made.
        """
        seen: dict[int, dict[str, Any]] = {}
        for code in codes:
            if code not in FREE_COMPETITIONS:
                continue
            try:
                for team in await self.competition_teams(code):
                    seen.setdefault(team["id"], team)
            except FootballAPIError as exc:
                log.warning("could not list teams for %s: %s", code, exc)
        return sorted(seen.values(), key=lambda t: (t["name"] or "").lower())


def window(
    days_back: int = 0, days_ahead: int = 7, *, today: date | None = None
) -> tuple[date, date]:
    """Inclusive (dateFrom, dateTo) pair for the API's date filters."""
    base = today or date.today()
    return base - timedelta(days=days_back), base + timedelta(days=days_ahead)
