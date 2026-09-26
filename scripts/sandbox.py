#!/usr/bin/env python3
"""Run the web app against a throwaway copy of the database.

    python scripts/sandbox.py              # fresh copy, then serve it on :8001
    python scripts/sandbox.py --keep       # carry on with last time's sandbox
    python scripts/sandbox.py --copy-only  # just make the copy

For driving an evening end to end — Start, everyone's cards, Close — without
any of it landing in the cellar. Deleting a test event already takes its
bottles and its votes with it, by cascade; **Close does not come back**. It
copies bottles into `wines` and cards into `wine_ratings`, and undoing that is
hand work. So the app gets pointed somewhere else entirely.

The copy holds the real 140 evenings, 1172 bottles and nine members, so what
you are testing looks like the club rather than like a fixture. Nothing you
press can reach `data/fcvino.sqlite3`. When you are done, delete one file.

It serves on 8001 rather than 8000 so it can sit beside the real app and the
port in the address bar tells you which one you are looking at.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SANDBOX = REPO / "data" / "sandbox.sqlite3"
DEFAULT_PORT = 8001


class WouldClobberTheRealThing(Exception):
    """The one mistake that would make this tool worse than useless."""


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


def sandbox_env(database: Path) -> dict[str, str]:
    """The environment the sandbox app runs in: the copy, and no Access.

    `FCVINO_ACCESS` is forced off, for two reasons that point the same way.
    With it on the session cookie is marked Secure, and a browser throws away a
    Secure cookie sent over plain http — so you sign in, get redirected, and
    are asked to sign in again, for ever. And nothing stands in front of this
    port: the `Cf-Access-Authenticated-User-Email` header it would otherwise
    believe could be set by anybody who can reach it. Trusting Cloudflare only
    makes sense where Cloudflare is the only way in, which is exactly what
    web/access.py says.
    """
    return {
        **os.environ,
        "FCVINO_DB_PATH": str(database),
        "FCVINO_ACCESS": "0",
    }


def serve(database: Path, port: int) -> int:
    """Hand the app the copy and get out of the way.

    Same interpreter, so it picks up this virtualenv without anyone having to
    remember to activate it, and `--reload` because this is a thing to poke at.
    """
    env = sandbox_env(database)
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "web.app:app",
         "--port", str(port), "--reload"],
        cwd=REPO, env=env, check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, default=DEFAULT_SANDBOX)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keep", action="store_true",
                        help="carry on with the existing sandbox instead of a fresh copy")
    parser.add_argument("--copy-only", action="store_true", help="make the copy and stop")
    args = parser.parse_args(argv)

    cfg = config.load(require_discord=False)
    live = Path(cfg.db_path).resolve()

    if args.keep and args.sandbox.exists():
        print(f"carrying on with {args.sandbox}")
    else:
        try:
            copy_database(live, args.sandbox)
        except WouldClobberTheRealThing as exc:
            print(exc)
            return 2
        print(f"copied {live}\n    to {args.sandbox}")

    # Said plainly and every time, because the whole value of this is knowing
    # which database you are about to press Close on.
    print(f"\nthe app below is on {args.sandbox} — the real one is untouched")
    if args.copy_only:
        print(f"run it yourself with:  FCVINO_DB_PATH={args.sandbox} "
              f"FCVINO_ACCESS=0 uvicorn web.app:app --port {args.port}")
        return 0
    print(f"http://localhost:{args.port}    (ctrl-c to stop)\n")
    return serve(args.sandbox, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
