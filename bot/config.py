"""Environment configuration, validated once at startup.

A missing token should produce a sentence you can act on, not a stack trace
five frames deep in the gateway code.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Competitions covered by the football-data.org free tier ("TIER_ONE").
FREE_COMPETITIONS: dict[str, str] = {
    "PL": "Premier League",
    "ELC": "Championship",
    "CL": "UEFA Champions League",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "PD": "La Liga",
    "FL1": "Ligue 1",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "BSA": "Brasileirão Série A",
    "EC": "European Championship",
    "WC": "FIFA World Cup",
}


class ConfigError(RuntimeError):
    """Raised when the environment is not usable. The message is user-facing."""


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int
    football_token: str | None
    db_path: Path
    tz: ZoneInfo
    reminder_lead_minutes: int
    reminder_competitions: tuple[str, ...]
    prediction_competition: str
    log_level: str
    web_password: str | None
    web_secret: str
    access_trusted: bool
    access_members: dict[str, str]

    @property
    def has_football(self) -> bool:
        return bool(self.football_token)


def _require(name: str, hint: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set in your .env file.\n  -> {hint}")
    return value


def _flag(name: str) -> bool:
    """A switch that is off unless it is turned on in so many words."""
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _email_map(name: str) -> dict[str, str]:
    """`alice@example.com=Alice, bob@example.com=Bob` into {email: name}.

    Lives in the environment rather than the database because the repository is
    public and these are nine real people's addresses. A malformed pair is
    skipped rather than fatal: a typo in one address should not take the whole
    site down at start-up.
    """
    pairs: dict[str, str] = {}
    for chunk in (os.getenv(name) or "").split(","):
        email, _, member = chunk.partition("=")
        email, member = email.strip().lower(), member.strip()
        if email and member:
            pairs[email] = member
    return pairs


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc


def load(*, require_discord: bool = True, require_web: bool = False) -> Config:
    """Read .env plus the process environment into a validated Config.

    `require_discord` and `require_web` say which half of the app is starting:
    the bot needs a Discord token, the web app needs a club password, and the
    scripts need neither. Everything else is shared, which is the point — both
    halves must agree on which database file they are talking to.
    """
    load_dotenv(REPO_ROOT / ".env")

    if require_discord:
        token = _require(
            "DISCORD_TOKEN",
            "Developer Portal -> your application -> Bot -> Reset Token, then paste it in .env",
        )
        guild_raw = _require(
            "DISCORD_GUILD_ID",
            "Enable Developer Mode in Discord, right-click the FC Vino server, Copy Server ID",
        )
        try:
            guild_id = int(guild_raw)
        except ValueError as exc:
            raise ConfigError(
                f"DISCORD_GUILD_ID must be the numeric server ID, got {guild_raw!r}. "
                "Right-click the server name and pick 'Copy Server ID' (needs Developer Mode)."
            ) from exc
    else:
        token = (os.getenv("DISCORD_TOKEN") or "").strip()
        guild_id = _int("DISCORD_GUILD_ID", 0)

    db_raw = (os.getenv("FCVINO_DB_PATH") or "data/fcvino.sqlite3").strip()
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    tz_name = (os.getenv("FCVINO_TZ") or "Europe/Oslo").strip()
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"FCVINO_TZ={tz_name!r} is not a known timezone name.") from exc

    reminder_raw = (os.getenv("REMINDER_COMPETITIONS") or "PL,CL").strip()
    reminder_comps = tuple(
        code for code in (c.strip().upper() for c in reminder_raw.split(",")) if code
    )
    unknown = [c for c in reminder_comps if c not in FREE_COMPETITIONS]
    if unknown:
        raise ConfigError(
            f"REMINDER_COMPETITIONS contains codes outside the free tier: {', '.join(unknown)}.\n"
            f"  -> pick from: {', '.join(sorted(FREE_COMPETITIONS))}"
        )

    prediction_comp = (os.getenv("PREDICTION_COMPETITION") or "PL").strip().upper()
    if prediction_comp not in FREE_COMPETITIONS:
        raise ConfigError(
            f"PREDICTION_COMPETITION={prediction_comp!r} is not in the free tier.\n"
            f"  -> pick from: {', '.join(sorted(FREE_COMPETITIONS))}"
        )

    # Set FCVINO_ACCESS once the app sits behind Cloudflare Access, which then
    # does the signing in. See web/access.py for why this is opt-in and not
    # simply "trust the header if it is there".
    access_trusted = _flag("FCVINO_ACCESS")
    access_members = _email_map("FCVINO_ACCESS_MEMBERS")

    web_password = (os.getenv("WEB_PASSWORD") or "").strip() or None
    if require_web and not web_password and not access_trusted:
        raise ConfigError(
            "WEB_PASSWORD is not set in your .env file.\n"
            "  -> pick a passphrase and share it with the club: WEB_PASSWORD=some words\n"
            "  -> or set FCVINO_ACCESS=1 if Cloudflare Access is the front door"
        )

    # A generated secret is fine for a localhost app — it only means everyone is
    # signed out when the server restarts. Set WEB_SESSION_SECRET to keep
    # sessions across restarts, and do set it once this is hosted anywhere.
    web_secret = (os.getenv("WEB_SESSION_SECRET") or "").strip() or secrets.token_hex(32)

    return Config(
        discord_token=token,
        guild_id=guild_id,
        football_token=(os.getenv("FOOTBALL_DATA_TOKEN") or "").strip() or None,
        db_path=db_path,
        tz=tz,
        reminder_lead_minutes=_int("REMINDER_LEAD_MINUTES", 60),
        reminder_competitions=reminder_comps,
        prediction_competition=prediction_comp,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        web_password=web_password,
        web_secret=web_secret,
        access_trusted=access_trusted,
        access_members=access_members,
    )
