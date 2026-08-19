"""Config validation: the error messages are the feature."""

from __future__ import annotations

import pytest

from bot import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Never let a real .env leak into these assertions.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)
    for name in (
        "DISCORD_TOKEN",
        "DISCORD_GUILD_ID",
        "FOOTBALL_DATA_TOKEN",
        "FCVINO_DB_PATH",
        "FCVINO_TZ",
        "REMINDER_LEAD_MINUTES",
        "REMINDER_COMPETITIONS",
        "PREDICTION_COMPETITION",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_token_names_the_variable_and_where_to_get_it():
    with pytest.raises(config.ConfigError) as excinfo:
        config.load()
    message = str(excinfo.value)
    assert "DISCORD_TOKEN" in message
    assert "Reset Token" in message


def test_missing_guild_id_is_reported_once_the_token_is_there(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load()
    assert "DISCORD_GUILD_ID" in str(excinfo.value)


def test_a_non_numeric_guild_id_explains_copy_server_id(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DISCORD_GUILD_ID", "FC Vino")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load()
    assert "Copy Server ID" in str(excinfo.value)


def test_defaults_are_sensible(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DISCORD_GUILD_ID", "42")
    cfg = config.load()
    assert cfg.guild_id == 42
    assert cfg.tz.key == "Europe/Oslo"
    assert cfg.reminder_lead_minutes == 60
    assert cfg.reminder_competitions == ("PL", "CL")
    assert cfg.prediction_competition == "PL"
    assert cfg.has_football is False
    assert cfg.db_path.is_absolute()


def test_football_token_flips_the_feature_on(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DISCORD_GUILD_ID", "42")
    monkeypatch.setenv("FOOTBALL_DATA_TOKEN", "k")
    assert config.load().has_football is True


def test_check_mode_tolerates_absent_discord_credentials():
    cfg = config.load(require_discord=False)
    assert cfg.discord_token == "" and cfg.guild_id == 0


def test_reminder_competitions_are_parsed_and_upper_cased(monkeypatch):
    monkeypatch.setenv("REMINDER_COMPETITIONS", " pl , cl ,sa ")
    assert config.load(require_discord=False).reminder_competitions == ("PL", "CL", "SA")


def test_a_paid_tier_competition_is_rejected_with_the_valid_list(monkeypatch):
    monkeypatch.setenv("REMINDER_COMPETITIONS", "PL,BL2")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(require_discord=False)
    assert "BL2" in str(excinfo.value) and "PL" in str(excinfo.value)


def test_an_unknown_prediction_competition_is_rejected(monkeypatch):
    monkeypatch.setenv("PREDICTION_COMPETITION", "nope")
    with pytest.raises(config.ConfigError):
        config.load(require_discord=False)


def test_a_bad_timezone_is_rejected(monkeypatch):
    monkeypatch.setenv("FCVINO_TZ", "Mars/Olympus_Mons")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(require_discord=False)
    assert "FCVINO_TZ" in str(excinfo.value)


def test_a_non_numeric_lead_time_is_rejected(monkeypatch):
    monkeypatch.setenv("REMINDER_LEAD_MINUTES", "an hour")
    with pytest.raises(config.ConfigError):
        config.load(require_discord=False)


def test_a_relative_db_path_resolves_from_the_repo_root(monkeypatch):
    monkeypatch.setenv("FCVINO_DB_PATH", "data/other.sqlite3")
    cfg = config.load(require_discord=False)
    assert cfg.db_path == config.REPO_ROOT / "data" / "other.sqlite3"
