"""The throwaway copy: faithful to the real database, and severed from it.

Both halves matter. A copy that is missing the last evening is misleading, and
a copy that is still somehow attached to the original is worse than no sandbox
at all — the point of it is that Close, the one irreversible button in the app,
cannot reach anything that matters.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.db import Database, utcnow_iso
from scripts.sandbox import WouldClobberTheRealThing, copy_database


@pytest.fixture()
def live(tmp_path):
    """A small database standing in for the real one, in WAL mode as it is."""
    db = Database(tmp_path / "live.sqlite3")
    db.connect()
    db.execute("INSERT INTO wine_members (name) VALUES ('Tore')")
    db.execute(
        "INSERT INTO tastings (key, year, month, theme, added_at) VALUES (?,?,?,?,?)",
        ("2019-03|barolo", 2019, 3, "Barolo", utcnow_iso()),
    )
    db.execute(
        """INSERT INTO wines (name, tasting_id, added_by, added_at) VALUES (?,1,0,?)""",
        ("Massolino Barolo 2015", utcnow_iso()),
    )
    db.execute(
        "INSERT INTO wine_ratings (wine_id, member_id, score, rated_at) VALUES (1,1,88,?)",
        (utcnow_iso(),),
    )
    yield db
    db.close()


def counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tastings", "wines", "wine_ratings", "wine_members")
        }
    finally:
        con.close()


def test_the_copy_holds_what_the_original_holds(live, tmp_path):
    """Including writes still sitting in the WAL, which a `cp` can miss."""
    sandbox = copy_database(live.path, tmp_path / "sandbox.sqlite3")
    assert counts(sandbox) == counts(live.path)
    assert counts(sandbox)["wine_ratings"] == 1


def test_writing_to_the_sandbox_leaves_the_real_one_alone(live, tmp_path):
    """The whole claim, in one test."""
    sandbox = copy_database(live.path, tmp_path / "sandbox.sqlite3")
    playing = Database(sandbox)
    playing.connect()
    playing.execute(
        "INSERT INTO tastings (key, year, theme, added_at) VALUES ('junk',2099,'Junk',?)",
        (utcnow_iso(),),
    )
    playing.execute("UPDATE wine_ratings SET score=10 WHERE wine_id=1 AND member_id=1")
    playing.close()

    assert sqlite3.connect(str(sandbox)).execute(
        "SELECT score FROM wine_ratings").fetchone()[0] == 10

    assert counts(sandbox)["tastings"] == 2
    assert counts(live.path)["tastings"] == 1
    assert live.query_one("SELECT score FROM wine_ratings")["score"] == 88


def test_a_second_copy_starts_from_the_real_data_again(live, tmp_path):
    """Fresh by default: a test run wants a clean slate, not yesterday's mess."""
    sandbox = copy_database(live.path, tmp_path / "sandbox.sqlite3")
    playing = Database(sandbox)
    playing.connect()
    playing.execute(
        "INSERT INTO tastings (key, year, theme, added_at) VALUES ('junk',2099,'Junk',?)",
        (utcnow_iso(),),
    )
    playing.close()
    assert counts(sandbox)["tastings"] == 2

    copy_database(live.path, tmp_path / "sandbox.sqlite3")
    assert counts(sandbox)["tastings"] == 1


def test_it_refuses_to_copy_over_the_real_database(live):
    """The one mistake that would make this tool worse than useless."""
    with pytest.raises(WouldClobberTheRealThing):
        copy_database(live.path, live.path)
    assert counts(live.path)["tastings"] == 1


def test_a_database_that_is_not_there(tmp_path):
    with pytest.raises(FileNotFoundError):
        copy_database(tmp_path / "nothing.sqlite3", tmp_path / "sandbox.sqlite3")


# -- the environment it serves in -------------------------------------------


def test_the_sandbox_points_the_app_at_the_copy(tmp_path):
    from scripts.sandbox import sandbox_env

    assert sandbox_env(tmp_path / "sandbox.sqlite3")["FCVINO_DB_PATH"] == str(
        tmp_path / "sandbox.sqlite3"
    )


def test_access_is_off_in_the_sandbox(monkeypatch, tmp_path):
    """Two reasons pointing the same way, and one of them you can see.

    With Access on, the session cookie is marked Secure, and a browser drops a
    Secure cookie sent over plain http — so you sign in, get redirected, and
    are asked to sign in again, for ever. (curl does not enforce Secure, which
    is why only a person clicking would ever find this.) The other reason is
    that nothing stands in front of this port, so the Cloudflare header it
    would otherwise believe could be set by anyone who can reach it.
    """
    from scripts.sandbox import sandbox_env

    monkeypatch.setenv("FCVINO_ACCESS", "1")
    assert sandbox_env(tmp_path / "sandbox.sqlite3")["FCVINO_ACCESS"] == "0"


def test_the_cookie_a_browser_would_keep(tmp_path, monkeypatch):
    """The end of it: over plain http the session cookie must not be Secure."""
    import dataclasses

    from fastapi.testclient import TestClient

    from bot import config
    from web.app import create_app

    Database(tmp_path / "sandbox.sqlite3").connect().close()
    cfg = dataclasses.replace(
        config.load(require_discord=False),
        db_path=tmp_path / "sandbox.sqlite3",
        web_password="grenache noir",
        web_secret="test-secret-not-a-real-one",
        football_token=None,
        anthropic_key=None,
        access_members={},
        access_trusted=False,        # what sandbox_env brings about
    )
    with TestClient(create_app(cfg)) as client:
        response = client.post("/login", data={"password": "grenache noir"},
                               follow_redirects=False)
    # Lower-cased before the check: Starlette writes the flag as `secure`, and
    # a test looking for `Secure` passes whether the flag is there or not.
    cookie = response.headers["set-cookie"].lower()
    assert "fcvino=" in cookie
    assert "secure" not in cookie, "a browser would throw this away over http"
