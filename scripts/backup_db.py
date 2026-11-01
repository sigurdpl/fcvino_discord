#!/usr/bin/env python3
"""Pull a verified copy of the club's archive off the server.

    python scripts/backup_db.py                    # take one now
    python scripts/backup_db.py --list             # what is already here
    python scripts/backup_db.py --dry-run          # say what it would do

The site runs on the Linux machine; this Mac keeps the history. **The Mac
reaches out** rather than the server pushing, for one reason: the Linux box is
a work computer that could be reclaimed, re-imaged or carried back to an office
without notice, and whatever can write to the backups can also destroy them.
It is given no way to reach this machine.

Three things make this a backup rather than a copied file:

**The snapshot is taken on the server, through sqlite.** The database runs in
WAL mode, so recent writes may still be in `-wal` beside it; a file copy takes
the past and says nothing. What crosses the network is a settled file nothing
is writing to.

**Every copy is checked before the old ones are pruned.** `PRAGMA
integrity_check`, and the row counts compared against the live database it came
from. A backup nobody verified is a rumour, and the night you need it is the
wrong time to find out.

**Pruning keeps the shape, not just the last few.** The most recent fourteen,
and the first of each month for ever. Fourteen days covers "I broke it
yesterday"; the monthlies cover "this has been wrong since the spring".

Where the server is comes from `--host` or `FCVINO_SERVER`, e.g.
`sigurd@mint.local`. Set up the key once with `ssh-copy-id`; the README has it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.remote import (  # noqa: E402
    RemoteTrouble,
    Server,
    integrity,
    local_counts,
)

REPO = Path(__file__).resolve().parent.parent
BACKUPS = REPO / "data" / "backups"
STAMP = "%Y-%m-%dT%H%M%S"
NAME = re.compile(r"^fcvino-(\d{4}-\d{2}-\d{2})T\d{6}\.sqlite3$")

KEEP_RECENT = 14          # the last fortnight, whatever else happens
# The first backup of any month is never deleted. Fourteen days answers "I
# broke it yesterday"; only the monthlies answer "this has been wrong since
# the spring", which is the failure nobody notices in time.


def taken_on(path: Path) -> str | None:
    found = NAME.match(path.name)
    return found.group(1) if found else None


def existing(folder: Path) -> list[Path]:
    return sorted((p for p in folder.glob("fcvino-*.sqlite3") if taken_on(p)),
                  key=lambda p: p.name)


def to_prune(backups: list[Path], keep_recent: int = KEEP_RECENT) -> list[Path]:
    """Which of these may go: not the recent ones, and never a month's first.

    Separated out and tested on its own, because deleting the wrong file here
    is the one mistake in this script that cannot be undone by running it
    again.
    """
    keep = set(backups[-keep_recent:]) if keep_recent else set()
    firsts: dict[str, Path] = {}
    for backup in backups:                       # sorted, so the first wins
        firsts.setdefault(taken_on(backup)[:7], backup)
    keep |= set(firsts.values())
    return [b for b in backups if b not in keep]


def differences(here: dict[str, int], there: dict[str, int]) -> list[str]:
    """Tables whose counts disagree, said in a way a person can act on."""
    return [f"{table}: {there.get(table, '?')} on the server, {here.get(table, '?')} here"
            for table in sorted(set(here) | set(there))
            if here.get(table) != there.get(table)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("FCVINO_SERVER"),
                        help="user@hostname of the machine serving the club")
    parser.add_argument("--path", default=os.getenv("FCVINO_SERVER_PATH", "~/fcvino_discord"),
                        help="where the repo lives there")
    parser.add_argument("--into", type=Path, default=BACKUPS)
    parser.add_argument("--keep", type=int, default=KEEP_RECENT,
                        help=f"how many recent backups to keep (default {KEEP_RECENT})")
    parser.add_argument("--list", action="store_true", help="show what is here and stop")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen")
    args = parser.parse_args(argv)

    if args.list:
        here = existing(args.into)
        if not here:
            print(f"no backups in {args.into}")
            return 0
        print(f"{len(here)} backup(s) in {args.into}:")
        for backup in here:
            print(f"  {backup.name:42} {backup.stat().st_size / 1024:>7.0f} KB")
        goes = to_prune(here, args.keep)
        print(f"\nnext run would prune {len(goes)}: "
              + (", ".join(p.name for p in goes) if goes else "nothing"))
        return 0

    if not args.host:
        print("No server given. Pass --host sigurd@mint.local, or set FCVINO_SERVER.",
              file=sys.stderr)
        return 2

    server = Server(host=args.host, path=args.path)
    stamp = datetime.now().strftime(STAMP)
    landing = args.into / f"fcvino-{stamp}.sqlite3"
    staging = f"/tmp/fcvino-backup-{stamp}.sqlite3"

    if args.dry_run:
        print(f"would snapshot {server.remote_path(server.database)} on {args.host}")
        print(f"            to {landing}")
        goes = to_prune(existing(args.into) + [landing], args.keep)
        print(f"   and prune {len(goes)}: "
              + (", ".join(p.name for p in goes) if goes else "nothing"))
        return 0

    try:
        if not server.exists(server.database):
            raise RemoteTrouble(
                f"no database at {server.remote_path(server.database)} on {args.host}.\n"
                "Has the site been moved there yet? --path says where to look.")

        print(f"snapshotting {server.remote_path(server.database)} on {args.host}")
        server.snapshot(staging)
        try:
            live = server.counts(server.database)
            server.fetch(staging, landing)
        finally:
            server.remove(staging)          # even if the copy failed
    except RemoteTrouble as exc:
        print(exc, file=sys.stderr)
        return 2

    # Checked before anything is pruned, so a bad copy can never be the reason
    # a good one was deleted.
    verdict = integrity(landing)
    if verdict != "ok":
        landing.unlink(missing_ok=True)
        print(f"the copy failed its integrity check ({verdict}); it has been "
              "deleted and nothing was pruned.", file=sys.stderr)
        return 2

    mine = local_counts(landing)
    drift = differences(mine, live)
    if drift:
        landing.unlink(missing_ok=True)
        print("the copy does not match the live database — kept nothing:",
              file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        return 2

    print(f"  {landing.name}  {landing.stat().st_size / 1024:.0f} KB")
    print(f"  verified: integrity ok, {mine['wines']} bottles, "
          f"{mine['wine_ratings']} ratings, {mine['tastings']} tastings")

    for old in to_prune(existing(args.into), args.keep):
        old.unlink()
        print(f"  pruned {old.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
