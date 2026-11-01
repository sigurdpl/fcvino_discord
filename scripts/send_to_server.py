#!/usr/bin/env python3
"""Put everything the site needs, and git refuses to carry, onto the server.

    python scripts/send_to_server.py --dry-run     # look first, always
    python scripts/send_to_server.py               # do it

The repository holds the code. It deliberately holds none of this:

    .env                     the tokens, and who each member is
    data/fcvino.sqlite3      fifteen years of the club
    data/labels/             the club's own bottle photographs
    web/static/*.png|jpg     the wordmark and the photographs of us
    web/icons/               the home-screen icons

So a fresh `git clone` on the Linux box runs, and runs empty. This carries the
rest across.

**It will not send the tunnel's credentials.** `~/.cloudflared/` has to go too
or the server cannot answer for app.fcvino.no at all — but it is the one secret
whose copy should be a deliberate act by a person, not a line in a script that
gets run again six months later by habit. The README says how.

**The database is the dangerous part.** Once the site is live on the server,
that machine's copy is the real one and this Mac's is yesterday's. Re-running
this then would overwrite an evening. So the database is sent only when the
server has none, or with `--force`, and `--force` prints what it is about to
destroy and counts to three.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.remote import RemoteTrouble, Server, local_counts  # noqa: E402
from scripts.serve import copy_database  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Everything gitignored that the site needs in order to be itself. Not the
# imports, the spreadsheets or the 30MB of 2012 photographs: those are already
# in the database, and they belong where they are.
ASSETS = (
    ".env",
    "web/icons/",
    "data/labels/",
)
PICTURES = ("web/static/", ("*.png", "*.jpg", "*.jpeg"))


def rsync(source: str, target: str, *extra: str, dry: bool = False) -> list[str]:
    return ["rsync", "-av", *(["--dry-run"] if dry else []), *extra, source, target]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("FCVINO_SERVER"),
                        help="user@hostname of the machine that will serve the club")
    parser.add_argument("--path", default=os.getenv("FCVINO_SERVER_PATH", "~/fcvino_discord"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-database", action="store_true",
                        help="overwrite the server's database with this Mac's")
    args = parser.parse_args(argv)

    if not args.host:
        print("No server given. Pass --host sigurd@mint.local, or set FCVINO_SERVER.",
              file=sys.stderr)
        return 2

    server = Server(host=args.host, path=args.path)
    there = f"{args.host}:{args.path.rstrip('/')}"

    try:
        # -- the small stuff, which is safe to send again any time -----------
        for asset in ASSETS:
            source = REPO / asset
            if not source.exists():
                print(f"  skipping {asset} — not on this machine")
                continue
            subprocess.run(rsync(str(source) + ("/" if asset.endswith("/") else ""),
                                 f"{there}/{asset}", dry=args.dry_run), check=True)

        folder, patterns = PICTURES
        includes = [f"--include={p}" for p in patterns]
        subprocess.run(
            rsync(f"{REPO / folder}/", f"{there}/{folder}",
                  "--include=*/", *includes, "--exclude=*", dry=args.dry_run),
            check=True)

        # -- the database, which is not ------------------------------------
        already = server.exists(server.database)
        if already and not args.force_database:
            counts = server.counts(server.database)
            mine = local_counts(REPO / "data" / "fcvino.sqlite3")
            print("\nThe server already has a database — NOT sending ours.")
            print(f"  there: {counts.get('wines')} bottles, "
                  f"{counts.get('wine_ratings')} ratings, {counts.get('events')} events")
            print(f"  here:  {mine.get('wines')} bottles, "
                  f"{mine.get('wine_ratings')} ratings, {mine.get('events')} events")
            print("\nOnce the site runs there, that copy is the real one and this\n"
                  "Mac's is yesterday's. If you are certain, --force-database.")
            return 0

        if already and args.force_database:
            counts = server.counts(server.database)
            print("\n--force-database will DESTROY the database on the server:")
            print(f"  {counts.get('wines')} bottles, {counts.get('wine_ratings')} "
                  f"ratings, {counts.get('events')} staged events")
            if input("Type the word 'overwrite' to continue: ").strip() != "overwrite":
                print("left alone.")
                return 1

        if args.dry_run:
            print("\nwould send data/fcvino.sqlite3 (through sqlite, not as a file)")
            return 0

        # Through sqlite, never as a file: WAL means a plain copy can be the
        # past. `scripts/serve.py:copy_database` is the one implementation.
        with tempfile.TemporaryDirectory() as tmp:
            settled = copy_database(REPO / "data" / "fcvino.sqlite3",
                                    Path(tmp) / "fcvino.sqlite3")
            server.ssh(f"mkdir -p {shlex.quote(server.remote_path('data'))}")
            subprocess.run(["scp", "-o", "BatchMode=yes", "-q", str(settled),
                            f"{args.host}:{server.remote_path(server.database)}"],
                           check=True)
        print("\nsent the database as a settled snapshot")

    except RemoteTrouble as exc:
        print(exc, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"{exc.cmd[0]} failed with {exc.returncode}", file=sys.stderr)
        return 2

    print("\nStill to do by hand, once:")
    print("  scp -r ~/.cloudflared <host>:~/      # the tunnel's credentials")
    print("  and stop the tunnel here before starting it there — one at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
