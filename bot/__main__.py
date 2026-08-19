"""Entry point: `python -m bot`.

`python -m bot --check` validates everything that can be validated without
talking to Discord — useful before you've pasted a token, and after any edit.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

import discord
from discord.ext import commands

from . import config
from .db import Database
from .football_api import FootballAPI

log = logging.getLogger("fcvino")


class _NoVoiceNoise(logging.Filter):
    """Drop discord.py's PyNaCl/davey warnings — this bot has no voice features."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()

CORE_COGS = ("bot.cogs.core", "bot.cogs.wine")
FOOTBALL_COGS = ("bot.cogs.football", "bot.cogs.predictions")


class FCVinoBot(commands.Bot):
    def __init__(self, cfg: config.Config) -> None:
        # Default intents only: slash commands need no privileged intent, so
        # there is nothing to enable in the Developer Portal.
        # when_mentioned rather than a "!" prefix: there are no text commands at
        # all, and anything else makes discord.py warn about the message content
        # intent we deliberately do not request.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        # One shared client, so the football/predictions cogs draw on the same
        # rate-limit budget and response cache.
        self.football = FootballAPI(cfg.football_token) if cfg.has_football else None

    async def setup_hook(self) -> None:
        self.db.connect()
        log.info("database ready at %s", self.db.path)

        extensions = list(CORE_COGS)
        if self.cfg.has_football:
            extensions += list(FOOTBALL_COGS)
        else:
            log.warning(
                "FOOTBALL_DATA_TOKEN not set — /football and /predict are disabled. "
                "Free key: https://www.football-data.org/client/register"
            )
        for ext in extensions:
            await self.load_extension(ext)
            log.info("loaded %s", ext)

        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("synced %d commands to guild %s", len(synced), self.cfg.guild_id)

    async def on_ready(self) -> None:
        log.info("logged in as %s (id %s)", self.user, getattr(self.user, "id", "?"))
        if self.get_guild(self.cfg.guild_id) is None:
            log.error(
                "I am not a member of guild %s. Re-run the OAuth2 invite URL with the "
                "'bot' and 'applications.commands' scopes and pick the FC Vino server.",
                self.cfg.guild_id,
            )
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="football & wine")
        )

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # pragma: no cover - defensive
        log.exception("command error", exc_info=error)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self.football is not None:
                await self.football.close()
            self.db.close()


def _check() -> int:
    """Offline self-test. Returns a process exit code."""
    ok = True
    try:
        cfg = config.load(require_discord=False)
        print("✓ config loads")
    except config.ConfigError as exc:
        print(f"✗ config: {exc}")
        return 1

    try:
        db = Database(cfg.db_path)
        db.connect()
        db.close()
        print(f"✓ database opens and migrates ({cfg.db_path})")
    except Exception as exc:  # noqa: BLE001 - report anything, keep checking
        print(f"✗ database: {exc}")
        ok = False

    for ext in CORE_COGS + FOOTBALL_COGS:
        try:
            importlib.import_module(ext)
            print(f"✓ {ext} imports")
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {ext}: {type(exc).__name__}: {exc}")
            ok = False

    print()
    missing = []
    if not cfg.discord_token:
        missing.append("DISCORD_TOKEN")
    if not cfg.guild_id:
        missing.append("DISCORD_GUILD_ID")
    if missing:
        print(f"• Not set yet: {', '.join(missing)} — see the README checklist.")
    else:
        print("• Discord credentials present.")
    print(
        "• Football data: key present."
        if cfg.has_football
        else "• Football data: no FOOTBALL_DATA_TOKEN, /football and /predict stay disabled."
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description="FC Vino Discord bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config, database and cogs without connecting to Discord",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check()

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"Configuration problem:\n\n{exc}\n", file=sys.stderr)
        print("Full setup steps are in README.md.", file=sys.stderr)
        return 2

    discord.utils.setup_logging(level=getattr(logging, cfg.log_level, logging.INFO))
    logging.getLogger("discord.client").addFilter(_NoVoiceNoise())
    bot = FCVinoBot(cfg)
    try:
        bot.run(cfg.discord_token, log_handler=None)
    except discord.LoginFailure:
        print(
            "Discord rejected that token.\n\n"
            "  -> Developer Portal -> your application -> Bot -> Reset Token, "
            "then copy the new value into .env (no quotes, no spaces).",
            file=sys.stderr,
        )
        return 2
    except discord.PrivilegedIntentsRequired:
        print(
            "Discord asked for a privileged intent this bot does not use. "
            "Check you have not enabled extra intents in the Developer Portal.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
