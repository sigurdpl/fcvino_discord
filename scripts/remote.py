"""Talking to the machine that serves the club, over ssh.

The web app is moving off this Mac and onto the Linux box at home. That makes
the Mac the place backups live rather than the place the site runs, and these
are the few primitives both directions need.

**The runner is injected**, the way `bot/vinmonopolet.py` injects its fetcher,
so the tests exercise every decision here without a network, a host, or a key.
Nothing in `tests/` opens an ssh connection.

**The database is never copied as a file.** It runs in WAL mode, so recent
writes can still be sitting in `-wal` beside it and a `cp` or an `rsync` of the
one file is a copy of the past — silently, and only for the evening you most
wanted. Every copy here goes through sqlite's own backup, which reads through
the same machinery the app does and writes one settled file.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Run on the server to make a consistent snapshot. Standard library only: the
# Linux box needs python3 and nothing else installed, not even the sqlite CLI.
SNAPSHOT = """
import sqlite3, sys
source = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
"""

# Counted on both sides after a copy, so "the backup is good" is measured
# rather than assumed.
TABLES = ("wines", "tastings", "wine_ratings", "events", "event_wines",
          "event_votes", "wine_members", "trips")

COUNTS = """
import sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for name in sys.argv[2:]:
    print(name, db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0])
"""


class RemoteTrouble(Exception):
    """Something about the server a sentence can explain."""


Runner = Callable[[Sequence[str]], str]


def _run(command: Sequence[str]) -> str:
    """Run it, return stdout, and turn a failure into a readable sentence."""
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RemoteTrouble(
            f"{command[0]} failed ({done.returncode}):\n"
            f"{(done.stderr or done.stdout).strip()}"
        )
    return done.stdout


@dataclass(frozen=True)
class Server:
    """The machine serving the club: where to reach it and where the app lives.

    `ssh` is given `BatchMode=yes` so a missing or wrong key fails at once with
    something to read, rather than hanging on a password prompt inside a
    scheduled job that nobody is watching.
    """

    host: str                                  # user@hostname
    path: str = "~/fcvino_discord"             # the repo on that machine
    database: str = "data/fcvino.sqlite3"      # relative to `path`
    run: Runner = _run

    def ssh(self, *command: str) -> str:
        return self.run(["ssh", "-o", "BatchMode=yes", self.host, *command])

    def python(self, source: str, *args: str) -> str:
        """Run a snippet of stdlib Python there, with no files left behind."""
        quoted = " ".join(shlex.quote(a) for a in args)
        return self.ssh(f"python3 -c {shlex.quote(source)} {quoted}")

    def remote_path(self, relative: str) -> str:
        """`path/relative`, left unquoted — callers quote where they use it."""
        return f"{self.path.rstrip('/')}/{relative}"

    def exists(self, relative: str) -> bool:
        out = self.ssh(
            f"test -e {shlex.quote(self.remote_path(relative))} && echo yes || echo no")
        return out.strip().endswith("yes")

    def snapshot(self, into: str) -> str:
        """Make a settled copy of the live database on the server itself.

        Returns the remote path it was written to. Doing this on the far side
        and copying the *result* is the whole point: what crosses the network
        is a file nothing is writing to.
        """
        self.python(SNAPSHOT, self.remote_path(self.database), into)
        return into

    def counts(self, relative_or_absolute: str) -> dict[str, int]:
        """Row counts for the tables that matter, from a database over there."""
        target = (relative_or_absolute if relative_or_absolute.startswith("/")
                  else self.remote_path(relative_or_absolute))
        out = self.python(COUNTS, target, *TABLES)
        found = {}
        for line in out.strip().splitlines():
            name, _, number = line.partition(" ")
            if number.strip().isdigit():
                found[name] = int(number)
        return found

    def fetch(self, remote: str, local: Path) -> Path:
        """Bring one file back. scp rather than rsync: it is one settled file."""
        local.parent.mkdir(parents=True, exist_ok=True)
        self.run(["scp", "-o", "BatchMode=yes", "-q",
                  f"{self.host}:{remote}", str(local)])
        return local

    def remove(self, remote: str) -> None:
        self.ssh(f"rm -f {shlex.quote(remote)}")


def local_counts(path: Path) -> dict[str, int]:
    """The same figures, for a database on this machine."""
    import sqlite3

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in TABLES}
    finally:
        db.close()


def integrity(path: Path) -> str:
    """sqlite's own verdict on the file, as a word."""
    import sqlite3

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        db.close()
