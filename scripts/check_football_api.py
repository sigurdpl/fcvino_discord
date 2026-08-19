#!/usr/bin/env python3
"""Verify FOOTBALL_DATA_TOKEN without going anywhere near Discord.

    python scripts/check_football_api.py           # next 10 days, prediction competition
    python scripts/check_football_api.py CL 30     # any free-tier code, any window
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.db import parse_utc  # noqa: E402
from bot.football_api import FootballAPI, FootballAPIError  # noqa: E402


async def main(argv: list[str]) -> int:
    try:
        cfg = config.load(require_discord=False)
    except config.ConfigError as exc:
        print(f"Configuration problem:\n{exc}")
        return 2

    if not cfg.has_football:
        print(
            "FOOTBALL_DATA_TOKEN is not set in .env.\n"
            "  -> free key: https://www.football-data.org/client/register"
        )
        return 2

    code = (argv[0].upper() if argv else cfg.prediction_competition)
    days = int(argv[1]) if len(argv) > 1 else 10
    if code not in config.FREE_COMPETITIONS:
        print(f"{code} is not in the free tier. Pick from: {', '.join(config.FREE_COMPETITIONS)}")
        return 2

    api = FootballAPI(cfg.football_token or "")
    try:
        today = date.today()
        matches = await api.competition_matches(
            code, date_from=today, date_to=today + timedelta(days=days)
        )
    except FootballAPIError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        await api.close()

    name = config.FREE_COMPETITIONS[code]
    print(f"✓ token accepted — {name} ({code})")
    print(f"  {len(matches)} match(es) in the next {days} day(s)\n")
    for match in matches[:20]:
        kickoff = parse_utc(match["kickoff_utc"]).astimezone(cfg.tz)
        score = ""
        if match["home_goals"] is not None:
            score = f"  {match['home_goals']}-{match['away_goals']}"
        print(
            f"  MD{match['matchday'] or '?':<3} {kickoff:%a %d %b %H:%M}  "
            f"{match['home']} vs {match['away']}  [{match['status']}]{score}"
        )
    if len(matches) > 20:
        print(f"  … and {len(matches) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
