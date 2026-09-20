"""The survey exports: what counts as a wine, and what counts as a score.

The files mix jokes in among the bottles — "Var Robert god i kveld?" — so the
rules that tell them apart are the ones worth pinning. Built from openpyxl
workbooks in a temp directory rather than fixtures on disk, so the club's own
scores stay out of the repository.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import openpyxl
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_surveys.py"
spec = importlib.util.spec_from_file_location("import_surveys", SCRIPT)
surveys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(surveys)

METADATA = ["Name", "Email", "Organisation", "Contact ID", "Date",
            "Completed", "Time Taken", "Country Code", "Region Code", "External ID"]


def export(tmp_path, name, questions, cards):
    """One survey export: `questions` are the headers, `cards` the rows.

    Each card is (submitted, [answers…], who).
    """
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "All Data"
    sheet.append(METADATA + questions)
    for submitted, answers, who in cards:
        sheet.append([None] * 4 + [submitted] + [None] * 5 + answers + [who])
    path = tmp_path / f"Export FC Vino {name}.xlsx"
    book.save(path)
    return path


def test_a_question_is_not_a_bottle(tmp_path):
    """The rule the whole import rests on: jokes end in a question mark."""
    path = export(
        tmp_path, "Jokes",
        ["1. Ch. Musar 2005", "2. Var Robert god i kveld?", "3. Navn"],
        [("2026-01-05 20:00:00", [88, "Ja"], "Morten")],
    )
    evening = surveys.read_export(path, [])
    assert [w["name"] for w in evening.wines] == ["Ch. Musar 2005"]


def test_a_stuck_key_is_read_as_what_it_meant(tmp_path):
    notes = []
    path = export(tmp_path, "Typo", ["1. Lussier Chenin 2022", "2. Navn"],
                  [("2026-01-05 20:00:00", [88787], "Sigurd")])
    evening = surveys.read_export(path, notes)
    assert evening.wines[0]["scores"] == {"Sigurd": 88}
    assert any("88787" in n and "88" in n for n in notes), "and says so"


def test_a_zero_is_not_a_verdict_of_nought(tmp_path):
    """The reading the workbook import already gives it: they weren't there."""
    path = export(tmp_path, "Zero", ["1. Ch. Musar 2005", "2. Navn"],
                  [("2026-01-05 20:00:00", [0], "Tore")])
    assert surveys.read_export(path, []).wines[0]["scores"] == {}


def test_the_later_card_wins(tmp_path):
    """Somebody re-submitting has usually just spotted a mistake."""
    path = export(
        tmp_path, "Twice", ["1. Ch. Musar 2005", "2. Navn"],
        [("2026-06-08 19:00:00", [70], "Sigurd"),
         ("2026-06-08 21:30:00", [90], "Sigurd")],
    )
    assert surveys.read_export(path, []).wines[0]["scores"] == {"Sigurd": 90}


def test_an_evening_is_dated_by_its_first_card(tmp_path):
    path = export(
        tmp_path, "When", ["1. Ch. Musar 2005", "2. Navn"],
        [("2026-02-26 09:00:00", [88], "Tore"),
         ("2026-02-25 21:00:00", [89], "Morten")],
    )
    assert surveys.read_export(path, []).when == datetime.date(2026, 2, 25)


def test_who_brought_it_when_that_is_all_the_survey_says(tmp_path):
    """"Min hvite favoritt" recorded the bringer, not the bottle."""
    path = export(tmp_path, "Min hvite favoritt",
                  ["1. Vin 1 ANDY", "2. Vin 2 RØD 1", "3. Navn"],
                  [("2025-06-10 20:00:00", [87, 85], "Tore")])
    wines = surveys.read_export(path, []).wines
    assert (wines[0]["name"], wines[0]["brought_by"]) == ("Andy's white", "Andy")
    assert (wines[1]["name"], wines[1]["brought_by"]) == ("Rød 1", None)


@pytest.mark.parametrize("filename, theme", [
    ("Export FC Vino Moden Piemonte", "Moden Piemonte"),
    ("Export FC Vino 03 2025 Tore i Sør-Amerika", "Tore i Sør-Amerika"),
    ("Export FC Vino januar 2025 Top of the Pops", "Top of the Pops"),
    ("Export FC Vino Sta. Rita Hills 13. mai 2026", "Sta. Rita Hills"),
    ("Export FC Vino Roberts våte drømmer 7_9-26", "Roberts våte drømmer"),
    ("Export FC Vino Top of the Pops 2025", "Top of the Pops 2025"),
])
def test_the_evening_is_named_by_its_file(filename, theme):
    """The year in 'Top of the Pops 2025' is the year reviewed, not the date,
    so it stays."""
    assert surveys.theme_from_filename(Path(f"{filename}.xlsx")) == theme


def test_excel_lock_files_are_not_spreadsheets(tmp_path):
    export(tmp_path, "Real", ["1. Ch. Musar 2005", "2. Navn"],
           [("2026-01-05 20:00:00", [88], "Tore")])
    (tmp_path / "~$Export FC Vino Real.xlsx").write_bytes(b"not a workbook")
    (tmp_path / "._Export FC Vino Real.xlsx").write_bytes(b"not a workbook")
    evenings = surveys.read_folder(tmp_path, [])
    assert [e.theme for e in evenings] == ["Real"]
