"""Where an evening was held, and who was responsible for it.

The club writes these down a line at a time, punctuated three different ways in
one file, so the parsing is worth pinning. So is the contract the trip pass
already keeps: safe to re-run, and never clobbers what a human typed.
"""

from __future__ import annotations

import pytest

from bot.db import utcnow_iso
from scripts.apply_tasting_details import (
    RESPONSIBLE,
    WHERE,
    Report,
    apply_file,
    read_file,
)


@pytest.fixture()
def evenings(db):
    """Three evenings a month apart, none of them saying where it was."""
    for key, year, month, theme in (
        ("2025-01|top of the pops", 2025, 1, "Top of the Pops"),
        ("2025-02|tore i sør-amerika", 2025, 2, "Tore i Sør-Amerika"),
        ("2026-08|priorato", 2026, 8, "Priorato"),
    ):
        db.execute(
            "INSERT INTO tastings (key, year, month, theme, added_at) VALUES (?,?,?,?,?)",
            (key, year, month, theme, utcnow_iso()),
        )
    return db


def place(db, year, month, column=WHERE):
    return db.query_one(
        f"SELECT {column} AS value FROM tastings WHERE year=? AND month=?", (year, month)
    )["value"]


def write(tmp_path, text):
    path = tmp_path / "where.txt"
    path.write_text(text, encoding="utf-8")
    return path


# -- the file ---------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("2023 - 12: Sigurd", (2023, 12, "Sigurd")),
    ("2025 01: Tore", (2025, 1, "Tore")),
    ("2026 04 Morten", (2026, 4, "Morten")),          # no colon at all
    ("  2025 09:Robert  ", (2025, 9, "Robert")),
    ("2025 05: Thomas & Sigurd", (2025, 5, "Thomas & Sigurd")),
])
def test_however_the_club_punctuated_it(tmp_path, line, expected):
    assert read_file(write(tmp_path, line), Report()) == [expected]


def test_blank_lines_and_comments_are_not_evenings(tmp_path):
    path = write(tmp_path, "2025 01: Tore\n\n# the rest to follow\n")
    report = Report()
    assert read_file(path, report) == [(2025, 1, "Tore")]
    assert report.problems == []


def test_a_line_that_is_not_an_evening_is_reported_not_skipped_silently(tmp_path):
    report = Report()
    assert read_file(write(tmp_path, "Tore's place, some time in May"), report) == []
    assert any("not a year" in p for p in report.problems)


def test_there_is_no_month_thirteen(tmp_path):
    report = Report()
    assert read_file(write(tmp_path, "2025 13: Tore"), report) == []
    assert any("month 13" in p for p in report.problems)


# -- writing it -------------------------------------------------------------


def test_the_evening_that_month_gets_the_name(evenings):
    report = apply_file(evenings, [(2025, 1, "Tore"), (2026, 8, "Thomas")], WHERE)
    assert place(evenings, 2025, 1) == "Tore"
    assert place(evenings, 2026, 8) == "Thomas"
    assert report.changes == 2


def test_responsible_is_a_different_column(evenings):
    apply_file(evenings, [(2025, 1, "Morten")], RESPONSIBLE)
    assert place(evenings, 2025, 1, RESPONSIBLE) == "Morten"
    assert place(evenings, 2025, 1, WHERE) is None, "where it was held is untouched"


def test_a_dry_run_writes_nothing(evenings):
    report = apply_file(evenings, [(2025, 1, "Tore")], WHERE, dry_run=True)
    assert report.changes == 1, "and still says what it would do"
    assert place(evenings, 2025, 1) is None


def test_running_it_twice_changes_nothing_the_second_time(evenings):
    apply_file(evenings, [(2025, 1, "Tore")], WHERE)
    again = apply_file(evenings, [(2025, 1, "Tore")], WHERE)
    assert again.changes == 0


def test_what_a_human_typed_is_kept(evenings):
    """The trip pass's contract, and the reason a re-run is safe to leave running."""
    apply_file(evenings, [(2025, 1, "Erk")], WHERE)
    report = apply_file(evenings, [(2025, 1, "Tore")], WHERE)
    assert place(evenings, 2025, 1) == "Erk"
    assert report.changes == 0
    assert any("keeping" in line and "Erk" in line for line in report.lines), "and says so"


def test_overwrite_lets_the_file_win(evenings):
    apply_file(evenings, [(2025, 1, "Erk")], WHERE)
    apply_file(evenings, [(2025, 1, "Tore")], WHERE, overwrite=True)
    assert place(evenings, 2025, 1) == "Tore"


def test_a_month_with_no_evening_is_a_problem_not_a_crash(evenings):
    report = apply_file(evenings, [(2025, 7, "Andy")], WHERE)
    assert any("no evening" in p for p in report.problems)
    assert report.changes == 0


def test_two_evenings_in_a_month_are_left_alone(evenings):
    """One a month is the club's habit, not a rule. Guessing would name the
    wrong one, and the file gives nothing to tell them apart."""
    evenings.execute(
        "INSERT INTO tastings (key, year, month, theme, added_at) VALUES (?,?,?,?,?)",
        ("2025-01|extra", 2025, 1, "An extra one", utcnow_iso()),
    )
    report = apply_file(evenings, [(2025, 1, "Tore")], WHERE)
    assert any("ambiguous" in p for p in report.problems)
    assert place(evenings, 2025, 1) is None
    assert report.changes == 0


def test_a_name_that_is_not_a_member_is_written_but_pointed_out(evenings):
    """"Alle" and "Thomas & Sigurd" are both already in this database, so an
    unfamiliar name is worth a word rather than a refusal."""
    report = apply_file(evenings, [(2025, 1, "Alle")], RESPONSIBLE)
    assert place(evenings, 2025, 1, RESPONSIBLE) == "Alle"
    assert any("not one member" in line for line in report.lines)
