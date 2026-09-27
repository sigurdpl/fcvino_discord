"""The throwaway copy: faithful to the real database, and severed from it.

Both halves matter. A copy that is missing the last evening is misleading, and
a copy that is still somehow attached to the original is worse than no sandbox
at all — the point of it is that Close, the one irreversible button in the app,
cannot reach anything that matters.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

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


# -- knowing which database you are looking at ------------------------------


def test_the_clubs_own_archive_shows_no_banner():
    """The half that matters. A strip that cannot switch itself off is a strip
    that will still be there in production, and then nobody reads it."""
    from bot.config import PRODUCTION_DB

    assert not _config_for(f"/anywhere/{PRODUCTION_DB}").sandbox


@pytest.mark.parametrize("name", ["sandbox.sqlite3", "restored.sqlite3", "copy.sqlite3"])
def test_any_other_database_is_a_sandbox(name):
    assert _config_for(f"/anywhere/{name}").sandbox


def _config_for(path):
    import dataclasses
    from pathlib import Path

    from bot import config

    return dataclasses.replace(config.load(require_discord=False), db_path=Path(path))


def test_the_banner_is_on_every_page_of_a_sandbox(tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from bot import config
    from web.app import create_app

    Database(tmp_path / "sandbox.sqlite3").connect().close()
    cfg = dataclasses.replace(
        config.load(require_discord=False), db_path=tmp_path / "sandbox.sqlite3",
        web_password="pw", web_secret="s", football_token=None, anthropic_key=None,
        access_trusted=False, access_members={},
    )
    with TestClient(create_app(cfg)) as client:
        client.post("/login", data={"password": "pw"})
        for path in ("/", "/wine", "/events", "/trips"):
            assert "Test data" in client.get(path).text, f"{path} is missing the strip"


def test_no_banner_when_it_is_the_real_thing(tmp_path):
    """Named as production, so the page must say nothing at all."""
    import dataclasses

    from fastapi.testclient import TestClient

    from bot import config
    from bot.config import PRODUCTION_DB
    from web.app import create_app

    Database(tmp_path / PRODUCTION_DB).connect().close()
    cfg = dataclasses.replace(
        config.load(require_discord=False), db_path=tmp_path / PRODUCTION_DB,
        web_password="pw", web_secret="s", football_token=None, anthropic_key=None,
        access_trusted=False, access_members={},
    )
    with TestClient(create_app(cfg)) as client:
        client.post("/login", data={"password": "pw"})
        assert "Test data" not in client.get("/wine").text


# -- behind the tunnel, Access is the point ---------------------------------


def test_access_is_left_alone_behind_cloudflare(monkeypatch, tmp_path):
    """There it is https, and Cloudflare is genuinely the only way in — so the
    header is what says which member is looking. Turning it off would ask the
    club for its password on top of Access."""
    from scripts.sandbox import sandbox_env

    monkeypatch.setenv("FCVINO_ACCESS", "1")
    behind = sandbox_env(tmp_path / "sandbox.sqlite3", behind_cloudflare=True)
    assert behind["FCVINO_ACCESS"] == "1", ".env decides"
    assert sandbox_env(tmp_path / "sandbox.sqlite3")["FCVINO_ACCESS"] == "0", "localhost does not"


# -- one front door, and the choice made out loud ---------------------------


def test_neither_flag_is_refused():
    """No default, because whichever way a default fell, somebody would one day
    serve the wrong database without having said so — and the two look exactly
    alike from the outside."""
    from scripts import serve

    with pytest.raises(SystemExit) as refused:
        serve.main([])
    assert refused.value.code != 0


def test_live_takes_no_copy_and_overrides_nothing(monkeypatch):
    """A live run cannot be talked into pointing somewhere else by a flag: it
    serves what .env names, with the environment exactly as it found it."""
    from scripts import serve

    monkeypatch.setenv("FCVINO_ACCESS", "1")
    assert serve.live_env() == dict(os.environ), "nothing added, nothing forced"

    copied, served = [], {}
    monkeypatch.setattr(serve, "copy_database",
                        lambda *a: copied.append(a) or Path("/nope"))
    monkeypatch.setattr(serve, "serve",
                        lambda database, port, env: served.update(db=database, env=env) or 0)

    assert serve.main(["--live", "--port", "8123"]) == 0
    assert copied == [], "a live run copies nothing"
    assert served["db"].name == "fcvino.sqlite3", "the club's own archive"
    assert served["env"]["FCVINO_ACCESS"] == "1", "off here would break the tunnel"


def test_the_default_ports_say_which_is_which(monkeypatch):
    """8000 is where the tunnel looks; a sandbox sits beside it on 8001."""
    from scripts import serve

    seen = {}
    monkeypatch.setattr(serve, "serve", lambda database, port, env: seen.update(port=port) or 0)
    monkeypatch.setattr(serve, "copy_database", lambda source, target: target)
    # The real ports, and the real app may well be sitting on them — this test
    # is about which number is chosen, not about whether it happens to be free.
    monkeypatch.setattr(serve, "who_has", lambda port: None)

    serve.main(["--live"])
    assert seen["port"] == serve.LIVE_PORT == 8000
    serve.main(["--sandbox"])
    assert seen["port"] == serve.SANDBOX_PORT == 8001


def test_the_old_command_still_means_sandbox(monkeypatch):
    """`python scripts/sandbox.py` is in the README and in your shell history."""
    from scripts import sandbox, serve

    seen = {}
    monkeypatch.setattr(serve, "serve",
                        lambda database, port, env: seen.update(db=database, env=env) or 0)
    monkeypatch.setattr(serve, "copy_database", lambda source, target: target)

    sandbox.main([])
    assert seen["db"].name == "sandbox.sqlite3"
    assert seen["env"]["FCVINO_ACCESS"] == "0", "still off on localhost"


# -- a port that is already taken -------------------------------------------


def test_a_busy_port_is_named_rather_than_thrown(monkeypatch):
    """uvicorn's own answer is `[Errno 48] Address already in use`, which names
    neither the port nor what has it. This app is started and restarted
    constantly across two ports, so the clash is worth answering properly."""
    import socket

    from scripts import serve

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        reached = []
        monkeypatch.setattr(serve, "serve", lambda *a, **k: reached.append(a) or 0)
        monkeypatch.setattr(serve, "copy_database", lambda source, target: target)

        assert serve.main(["--sandbox", "--port", str(port), "--keep"]) == 2
        assert serve.main(["--live", "--port", str(port)]) == 2
        assert reached == [], "uvicorn is never reached"

        message = serve.who_has(port)
        assert message and str(port) in message
    finally:
        held.close()


def test_a_free_port_gets_through(monkeypatch, tmp_path):
    import socket

    from scripts import serve

    with socket.socket() as probe:          # a port nobody is on, borrowed and let go
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert serve.who_has(port) is None

    served = {}
    monkeypatch.setattr(serve, "serve",
                        lambda database, p, env: served.update(port=p) or 0)
    monkeypatch.setattr(serve, "copy_database", lambda source, target: target)
    assert serve.main(["--sandbox", "--port", str(port), "--keep"]) == 0
    assert served["port"] == port


def test_the_clash_is_found_before_the_copy_is_taken(monkeypatch):
    """Otherwise a busy port would cost you the sandbox you were keeping."""
    import socket

    from scripts import serve

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        copied = []
        monkeypatch.setattr(serve, "copy_database", lambda *a: copied.append(a))
        monkeypatch.setattr(serve, "serve", lambda *a, **k: 0)
        assert serve.main(["--sandbox", "--port", str(port)]) == 2
        assert copied == [], "nothing was overwritten on the way to the refusal"
    finally:
        held.close()
