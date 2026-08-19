"""The API client's own logic: rate limiting, caching keys, and normalisation."""

from __future__ import annotations

import asyncio

import pytest

from bot.football_api import (
    FootballAPI,
    FootballAPIError,
    RateLimiter,
    normalize_match,
    window,
)

RAW_MATCH = {
    "id": 497001,
    "utcDate": "2026-08-21T19:00:00Z",
    "status": "TIMED",
    "matchday": 1,
    "competition": {"code": "PL", "name": "Premier League"},
    "homeTeam": {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal"},
    "awayTeam": {"id": 61, "name": "Chelsea FC", "shortName": "Chelsea"},
    "score": {"fullTime": {"home": None, "away": None}},
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_limiter_lets_the_budget_through_without_sleeping():
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        await clock.sleep(seconds)

    limiter = RateLimiter(8, 60, clock=clock, sleep=sleep)

    async def run():
        for _ in range(8):
            await limiter.acquire()

    asyncio.run(run())
    assert slept == []


def test_limiter_waits_out_the_window_when_the_budget_is_spent():
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        await clock.sleep(seconds)

    limiter = RateLimiter(2, 60, clock=clock, sleep=sleep)

    async def run():
        for _ in range(4):
            await limiter.acquire()
            clock.now += 1  # a request takes a moment

    asyncio.run(run())
    assert slept, "the fourth call must have waited"
    assert clock.now >= 60, "and it must not have exceeded 2 calls in any 60s window"


def test_normalize_match_flattens_to_the_mirror_shape():
    assert normalize_match(RAW_MATCH) == {
        "match_id": 497001,
        "competition": "PL",
        "matchday": 1,
        "home_id": 57,
        "home": "Arsenal",
        "away_id": 61,
        "away": "Chelsea",
        "kickoff_utc": "2026-08-21T19:00:00+00:00",
        "status": "TIMED",
        "home_goals": None,
        "away_goals": None,
    }


def test_normalize_match_reads_the_full_time_score():
    played = {
        **RAW_MATCH,
        "status": "FINISHED",
        "score": {"winner": "HOME_TEAM", "fullTime": {"home": 2, "away": 1}},
    }
    result = normalize_match(played)
    assert (result["home_goals"], result["away_goals"]) == (2, 1)


def test_normalize_match_survives_a_missing_score_block():
    bare = {k: v for k, v in RAW_MATCH.items() if k != "score"}
    result = normalize_match(bare)
    assert result["home_goals"] is None


def test_normalize_match_falls_back_to_the_passed_competition_code():
    without_competition = {k: v for k, v in RAW_MATCH.items() if k != "competition"}
    assert normalize_match(without_competition, "CL")["competition"] == "CL"


def test_normalize_match_uses_the_long_name_when_there_is_no_short_one():
    payload = {**RAW_MATCH, "homeTeam": {"id": 57, "name": "Arsenal FC"}}
    assert normalize_match(payload)["home"] == "Arsenal FC"


def test_normalize_match_labels_an_undrawn_side_as_tbd():
    payload = {**RAW_MATCH, "awayTeam": {"id": None}}
    assert normalize_match(payload)["away"] == "TBD"


def test_cache_key_is_order_independent():
    a = FootballAPI._cache_key("/x", {"dateFrom": "2026-01-01", "dateTo": "2026-01-08"})
    b = FootballAPI._cache_key("/x", {"dateTo": "2026-01-08", "dateFrom": "2026-01-01"})
    assert a == b


def test_cache_key_separates_different_params():
    a = FootballAPI._cache_key("/x", {"dateFrom": "2026-01-01"})
    b = FootballAPI._cache_key("/x", {"dateFrom": "2026-02-01"})
    assert a != b


class FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Just enough of aiohttp.ClientSession for FootballAPI._get."""

    closed = False

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return self._responses.pop(0)


def _api(session, clock):
    return FootballAPI(
        "token",
        session=session,
        clock=clock,
        sleep=clock.sleep,
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
    )


def test_a_successful_response_is_returned_and_cached():
    clock = FakeClock()
    session = FakeSession(FakeResponse(200, {"matches": [RAW_MATCH]}))
    api = _api(session, clock)

    async def run():
        first = await api._get("/competitions/PL/matches", {"dateFrom": "2026-08-19"}, ttl=600)
        second = await api._get("/competitions/PL/matches", {"dateFrom": "2026-08-19"}, ttl=600)
        return first, second

    first, second = asyncio.run(run())
    assert first == second == {"matches": [RAW_MATCH]}
    assert len(session.calls) == 1, "the second call must come from the cache"


def test_the_cache_expires_and_the_request_is_repeated():
    clock = FakeClock()
    session = FakeSession(FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2}))
    api = _api(session, clock)

    async def run():
        first = await api._get("/x", ttl=600)
        clock.now += 601
        second = await api._get("/x", ttl=600)
        return first, second

    assert asyncio.run(run()) == ({"n": 1}, {"n": 2})
    assert len(session.calls) == 2


def test_none_valued_params_are_dropped_from_the_query():
    clock = FakeClock()
    session = FakeSession(FakeResponse(200, {}))
    api = _api(session, clock)
    asyncio.run(api._get("/x", {"status": None, "dateFrom": "2026-08-19"}))
    assert session.calls[0][1] == {"dateFrom": "2026-08-19"}


def test_a_restricted_competition_explains_the_free_tier():
    clock = FakeClock()
    session = FakeSession(FakeResponse(403, {"message": "restricted"}))
    api = _api(session, clock)
    with pytest.raises(FootballAPIError) as excinfo:
        asyncio.run(api._get("/competitions/BL2/matches"))
    assert excinfo.value.status == 403
    assert "free" in str(excinfo.value).lower()


def test_a_server_error_is_reported_as_transient():
    clock = FakeClock()
    session = FakeSession(FakeResponse(503, {}))
    api = _api(session, clock)
    with pytest.raises(FootballAPIError) as excinfo:
        asyncio.run(api._get("/x"))
    assert excinfo.value.status == 503


def test_a_rate_limited_response_is_retried_once_then_reported():
    clock = FakeClock()
    session = FakeSession(
        FakeResponse(429, {"seconds_to_wait": 3}), FakeResponse(200, {"ok": True})
    )
    api = _api(session, clock)
    assert asyncio.run(api._get("/x")) == {"ok": True}
    assert len(session.calls) == 2
    assert clock.now >= 3, "it waited out the window the API asked for"


def test_a_failed_response_is_not_cached():
    clock = FakeClock()
    session = FakeSession(FakeResponse(500, {}), FakeResponse(200, {"ok": True}))
    api = _api(session, clock)

    async def run():
        with pytest.raises(FootballAPIError):
            await api._get("/x")
        return await api._get("/x")

    assert asyncio.run(run()) == {"ok": True}


def test_competition_matches_normalises_and_passes_the_date_filters():
    from datetime import date

    clock = FakeClock()
    session = FakeSession(FakeResponse(200, {"matches": [RAW_MATCH]}))
    api = _api(session, clock)
    matches = asyncio.run(
        api.competition_matches("PL", date_from=date(2026, 8, 19), date_to=date(2026, 8, 26))
    )
    assert session.calls[0][1] == {"dateFrom": "2026-08-19", "dateTo": "2026-08-26"}
    assert matches[0]["home"] == "Arsenal"


def test_selectable_teams_skips_a_competition_that_fails():
    clock = FakeClock()
    session = FakeSession(
        FakeResponse(200, {"teams": [{"id": 57, "name": "Arsenal FC", "shortName": "Arsenal"}]}),
        FakeResponse(403, {}),
    )
    api = _api(session, clock)
    teams = asyncio.run(api.selectable_teams(["PL", "CL"]))
    assert [t["id"] for t in teams] == [57]


def test_selectable_teams_ignores_codes_outside_the_free_tier():
    clock = FakeClock()
    session = FakeSession()
    api = _api(session, clock)
    assert asyncio.run(api.selectable_teams(["BL2"])) == []
    assert session.calls == []


def test_window_is_inclusive_around_today():
    from datetime import date

    lo, hi = window(3, 7, today=date(2026, 8, 19))
    assert (lo.isoformat(), hi.isoformat()) == ("2026-08-16", "2026-08-26")
