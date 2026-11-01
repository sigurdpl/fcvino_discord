"""Backing the club's archive off the machine that serves it.

Every decision here is tested without a network, a host or a key: `Server`
takes its runner the way `bot/vinmonopolet.py` takes its fetcher, so the tests
hand it a function that records commands and returns canned answers.

What is worth testing is narrow and sharp. Pruning deletes files and is the one
act in the backup that running it again cannot undo. Verification decides
whether a copy is kept at all. And the copy must go through sqlite rather than
the filesystem, because a WAL database copied as a file is quietly the past.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot.db import Database
from scripts.backup_db import differences, existing, taken_on, to_prune
from scripts.remote import SNAPSHOT, Server, integrity, local_counts


def dated(folder: Path, *stamps: str) -> list[Path]:
    """Backup files with the names the script gives them."""
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for stamp in stamps:
        p = folder / f"fcvino-{stamp}.sqlite3"
        p.write_bytes(b"")
        made.append(p)
    return made


# -- pruning: the one thing here that destroys something --------------------


def test_the_recent_ones_are_kept(tmp_path):
    days = [f"2026-03-{d:02d}T190000" for d in range(1, 21)]
    dated(tmp_path, *days)
    goes = to_prune(existing(tmp_path), keep_recent=14)
    kept = [p.name for p in existing(tmp_path) if p not in goes]
    assert len(kept) >= 14
    assert "fcvino-2026-03-20T190000.sqlite3" in kept, "the newest must survive"


def test_the_first_of_every_month_is_never_deleted(tmp_path):
    """Fourteen days answers "I broke it yesterday". Only the monthlies answer
    "this has been wrong since the spring", which is the one nobody notices."""
    dated(tmp_path,
          "2026-01-04T190000", "2026-01-19T190000",
          "2026-02-02T190000", "2026-02-27T190000",
          "2026-03-01T190000", *[f"2026-03-{d:02d}T190000" for d in range(2, 20)])
    goes = {p.name for p in to_prune(existing(tmp_path), keep_recent=5)}
    for first in ("fcvino-2026-01-04T190000.sqlite3",
                  "fcvino-2026-02-02T190000.sqlite3",
                  "fcvino-2026-03-01T190000.sqlite3"):
        assert first not in goes, f"{first} is a month's first and must be kept"
    assert "fcvino-2026-01-19T190000.sqlite3" in goes, "but the rest of January can go"


def test_nothing_is_pruned_when_there_is_little(tmp_path):
    dated(tmp_path, "2026-03-01T190000", "2026-03-02T190000")
    assert to_prune(existing(tmp_path), keep_recent=14) == []


def test_a_stray_file_is_not_treated_as_a_backup(tmp_path):
    """Something else in the folder must never be considered for deletion."""
    dated(tmp_path, "2026-03-01T190000")
    (tmp_path / "notes.txt").write_text("not a backup")
    (tmp_path / "fcvino-nonsense.sqlite3").write_bytes(b"")
    found = [p.name for p in existing(tmp_path)]
    assert found == ["fcvino-2026-03-01T190000.sqlite3"]
    assert taken_on(tmp_path / "fcvino-nonsense.sqlite3") is None


# -- verification: whether a copy is worth keeping --------------------------


@pytest.fixture()
def cellar(tmp_path):
    """A small real database, in WAL mode as the club's is."""
    db = Database(tmp_path / "live.sqlite3")
    db.connect()
    db.execute("INSERT INTO wine_members (name) VALUES ('Tore')")
    db.execute("INSERT INTO wines (name, added_by, added_at) "
               "VALUES ('Ch. Musar 2005', 1, '2026-01-01')")
    db.close()
    return tmp_path / "live.sqlite3"


def test_a_good_copy_passes_both_checks(cellar, tmp_path):
    copy = tmp_path / "copy.sqlite3"
    source = sqlite3.connect(f"file:{cellar}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    source.backup(target)
    target.close()
    source.close()

    assert integrity(copy) == "ok"
    assert differences(local_counts(copy), local_counts(cellar)) == []


def test_a_copy_that_lost_rows_is_reported_table_by_table(cellar, tmp_path):
    """The check that matters: a file can be perfectly valid sqlite and still
    not be the archive. Integrity alone would call this a good backup."""
    copy = tmp_path / "copy.sqlite3"
    source = sqlite3.connect(f"file:{cellar}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    source.backup(target)
    target.close()
    source.close()

    stale = sqlite3.connect(copy)
    stale.execute("DELETE FROM wines")
    stale.commit()
    stale.close()

    assert integrity(copy) == "ok", "still a valid file"
    drift = differences(local_counts(copy), local_counts(cellar))
    assert any("wines:" in line for line in drift)
    assert "1 on the server, 0 here" in " ".join(drift)


# -- the server, without a server -------------------------------------------


def recorder(answers=None):
    """A runner that records what it was asked and replies from a script."""
    said: list[list[str]] = []
    answers = list(answers or [])

    def run(command):
        said.append(list(command))
        return answers.pop(0) if answers else ""

    run.said = said                      # type: ignore[attr-defined]
    return run


def test_the_snapshot_is_taken_through_sqlite_on_the_far_side(tmp_path):
    """Not rsync, not cp. A WAL database copied as a file is the past, and the
    failure is silent — which is exactly the kind you only meet in anger."""
    run = recorder()
    server = Server(host="sigurd@mint.local", path="~/fcvino", run=run)
    server.snapshot("/tmp/snap.sqlite3")

    sent = " ".join(run.said[0])
    assert sent.startswith("ssh -o BatchMode=yes sigurd@mint.local")
    assert "sqlite3" in sent and "backup" in sent, "it uses sqlite's own backup"
    assert "rsync" not in sent and " cp " not in sent
    assert "~/fcvino/data/fcvino.sqlite3" in sent
    assert "/tmp/snap.sqlite3" in sent


def test_the_remote_snippet_needs_nothing_installed():
    """python3 and the standard library only: the Linux box should not have to
    grow dependencies to be backed up."""
    assert "import sqlite3" in SNAPSHOT
    for package in ("requests", "pip", "apt", "sqlalchemy"):
        assert package not in SNAPSHOT


def test_a_password_prompt_can_never_hang_a_scheduled_run():
    run = recorder()
    Server(host="h", run=run).ssh("true")
    assert "BatchMode=yes" in run.said[0], \
        "without it a missing key waits for a password nobody is there to type"


def test_counts_come_back_as_numbers():
    run = recorder(["wines 1172\nwine_ratings 8469\ntastings 140\n"])
    found = Server(host="h", run=run).counts("data/fcvino.sqlite3")
    assert found == {"wines": 1172, "wine_ratings": 8469, "tastings": 140}


def test_a_path_with_a_space_survives_the_journey():
    run = recorder()
    Server(host="h", path="~/my wines", run=run).snapshot("/tmp/s.sqlite3")
    assert "'~/my wines/data/fcvino.sqlite3'" in " ".join(run.said[0]) or \
           '"~/my wines/data/fcvino.sqlite3"' in " ".join(run.said[0])
