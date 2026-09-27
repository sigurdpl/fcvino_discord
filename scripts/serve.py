#!/usr/bin/env python3
"""Serve the web app, against the club's archive or against a copy of it.

    python scripts/serve.py --live
    python scripts/serve.py --sandbox
    python scripts/serve.py --sandbox --port 8000 --keep --behind-cloudflare

One front door for both, because the alternative was two differently shaped
commands — and the one needed on the day the site goes live is the one typed
least often.

**Neither is the default.** Whichever way a default fell, somebody would one
day serve the wrong database without having said so, and the two look exactly
alike from the outside. So the choice is made out loud or not at all; the
database is printed before anything is served either way.

Underneath all of it the page says which it is: `Config.sandbox` follows the
*file* rather than the command, so even a wrong flag announces itself on every
page. See `bot/config.py`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SANDBOX = REPO / "data" / "sandbox.sqlite3"

# The tunnel looks at 8000, so that is where the real thing belongs; a sandbox
# goes beside it on 8001, where the port itself says which one you are reading.
LIVE_PORT = 8000
SANDBOX_PORT = 8001


class WouldClobberTheRealThing(Exception):
    """The one mistake that would make a sandbox worse than useless."""


def copy_database(source: Path, target: Path) -> Path:
    """A faithful copy, through sqlite rather than the filesystem.

    `cp` of the one file is not enough: the database runs in WAL mode, so
    recent writes may still be sitting in `-wal` beside it, and a copy taken
    without them is a copy of the past. `Connection.backup()` reads through
    the same machinery the app does and writes one settled file.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise WouldClobberTheRealThing(
            f"{target} is the database itself — a sandbox has to be somewhere else."
        )
    if not source.exists():
        raise FileNotFoundError(f"no database at {source}")

    for leftover in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        leftover.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()
    return target


def sandbox_env(database: Path, *, behind_cloudflare: bool = False) -> dict[str, str]:
    """The environment a *sandbox* runs in: the copy, and usually no Access.

    `FCVINO_ACCESS` is forced off by default, for two reasons that point the
    same way. With it on the session cookie is marked Secure, and a browser
    throws away a Secure cookie sent over plain http — so you sign in, get
    redirected, and are asked to sign in again, for ever. And nothing stands in
    front of a localhost port: the `Cf-Access-Authenticated-User-Email` header
    it would otherwise believe could be set by anybody who can reach it.

    **Behind the tunnel both of those reverse.** It is https, so the cookie
    survives; and Cloudflare is genuinely the only way in, so the header is
    what tells the app which member is looking. Turning Access off there would
    ask the club for its password on top of Access and then make everyone pick
    their own name from a dropdown. So `--behind-cloudflare` leaves the setting
    alone and lets `.env` decide, which is what web/access.py means by trusting
    the header only where Cloudflare is the only way in.
    """
    env = {**os.environ, "FCVINO_DB_PATH": str(database)}
    if not behind_cloudflare:
        env["FCVINO_ACCESS"] = "0"
    return env


def live_env() -> dict[str, str]:
    """The environment the *real* app runs in: whatever `.env` already says.

    Nothing is overridden, which is the point — a live run cannot be talked
    into pointing somewhere else by a flag it was given.
    """
    return dict(os.environ)


def who_has(port: int) -> str | None:
    """A sentence naming whatever is already listening, or None if nothing is.

    uvicorn's own answer is `[Errno 48] Address already in use`, which names
    neither the port nor what has it — and this app is started and restarted
    constantly, two databases across two ports with a tunnel in front, so the
    clash is worth answering properly.
    """
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            pass
        else:
            return None

    if not shutil.which("lsof"):
        return f"Port {port} is already in use."
    found = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
        capture_output=True, text=True, check=False,
    ).stdout
    pid = next((line[1:] for line in found.splitlines() if line.startswith("p")), None)
    name = next((line[1:] for line in found.splitlines() if line.startswith("c")), "something")
    if not pid:
        return f"Port {port} is already in use."
    return f"Port {port} is already being served by {name} (pid {pid})."


def serve(database: Path, port: int, env: dict[str, str]) -> int:
    """Hand the app a database and get out of the way.

    Same interpreter, so it picks up this virtualenv without anyone having to
    remember to activate it, and `--reload` because this is still a thing being
    worked on.
    """
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "web.app:app",
         "--port", str(port), "--reload"],
        cwd=REPO, env=env, check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--live", action="store_true",
                       help="the club's own archive, as .env names it")
    which.add_argument("--sandbox", action="store_true",
                       help="a throwaway copy, safe to press anything in")
    parser.add_argument("--port", type=int, help="default 8000 live, 8001 sandbox")
    parser.add_argument("--sandbox-path", type=Path, default=DEFAULT_SANDBOX)
    parser.add_argument("--keep", action="store_true",
                        help="sandbox: carry on with the existing copy")
    parser.add_argument("--behind-cloudflare", action="store_true",
                        help="sandbox: served through the tunnel, so leave Access as .env has it")
    parser.add_argument("--copy-only", action="store_true",
                        help="sandbox: make the copy and stop")
    args = parser.parse_args(argv)

    cfg = config.load(require_discord=False)
    live = Path(cfg.db_path).resolve()

    if args.live:
        port = args.port or LIVE_PORT
        taken = who_has(port)
        if taken:
            print(f"{taken}\nStop it, or pass --port something else.")
            return 2
        # Said before it is served, every time. This is the club's real
        # fifteen years, and the page will not carry a banner to say so.
        print(f"serving THE CLUB'S OWN ARCHIVE — {live}")
        print(f"http://localhost:{port}    (ctrl-c to stop)\n")
        return serve(live, port, live_env())

    port = args.port or SANDBOX_PORT
    # Before the copy, so a clash does not cost you the sandbox you were keeping.
    taken = None if args.copy_only else who_has(port)
    if taken:
        print(f"{taken}\nStop it, or pass --port something else.")
        return 2
    if args.keep and args.sandbox_path.exists():
        print(f"carrying on with {args.sandbox_path}")
    else:
        try:
            copy_database(live, args.sandbox_path)
        except WouldClobberTheRealThing as exc:
            print(exc)
            return 2
        print(f"copied {live}\n    to {args.sandbox_path}")

    print(f"\nserving a COPY — {args.sandbox_path}. The real one is untouched,")
    print("and every page will say so.")
    if args.copy_only:
        access = "" if args.behind_cloudflare else "FCVINO_ACCESS=0 "
        print(f"run it yourself with:  FCVINO_DB_PATH={args.sandbox_path} "
              f"{access}uvicorn web.app:app --port {port}")
        return 0
    print(f"http://localhost:{port}    (ctrl-c to stop)\n")
    return serve(args.sandbox_path, port,
                 sandbox_env(args.sandbox_path, behind_cloudflare=args.behind_cloudflare))


if __name__ == "__main__":
    raise SystemExit(main())
