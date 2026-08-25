"""Reading the club's spreadsheet.

Every rule here was derived from the real workbook, so the cases are the actual
mess rather than invented ones: eight ways of writing a month, a year typed as
2323, two scoring scales, a header cell overwritten with a theme name, and two
tables sitting side by side on one sheet.
"""

from __future__ import annotations

import datetime

import pytest

from bot import vinotek

ALLTIME_HEADER = [
    "Tid", "Ansvarlig", "Tema", "Sted", "Vin", "Poeng",
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
]
SHEET_2013 = [
    "Ansvarlig", "Sted", "Tema", "Vin", "Poeng",
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
    "Marius", "Sum", "Max", "Min", "Antall", "Mat",
]
SHEET_2015 = [
    "Tid", "Ansvarlig", "Tema", "Sted", "Vin", "Pris", "Poeng",
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
]
SHEET_2016 = [
    # The theme column's header was overwritten with the name of a tasting, and
    # a second table sits to the right of the first.
    "Tid", "Ansvarlig", "Top of the pops", "Sted", "Vin", "Poeng",
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
    "", "Sum", "Max", "Min", "Antall", "", "",
    "Tid", "Ansvarlig", "Tema", "Sted", "Vin", "Poeng",
    "Andy", "Håvard", "Tore", "Robert", "Thomas", "Morten", "Sigurd", "Erk", "Lennart",
    "", "Sum",
]

ALLTIME = vinotek.SheetSpec("Alltime", 1, None)
EARLY = vinotek.SheetSpec("2014", 10, None)


# -- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("okt18", (2018, 10)),
        ("nov19", (2019, 11)),
        ("feb21", (2021, 2)),
        ("Sept19", (2019, 9)),          # four-letter abbreviation
        ("Okt20", (2020, 10)),          # capitalised
        ("april24", (2024, 4)),
        ("mars19", (2019, 3)),
        ("juli24", (2024, 7)),
        ("mai21", (2021, 5)),
        ("des18", (2018, 12)),
        ("April22", (2022, 4)),
        ("August 2021", (2021, 8)),     # full name, four-digit year
        ("Juli 2021", (2021, 7)),
        ("Setember 2021", (2021, 9)),   # misspelt in the sheet
    ],
)
def test_month_and_year_forms(raw, expected):
    assert vinotek.parse_period(raw) == expected


@pytest.mark.parametrize("raw,expected", [("0524", (2024, 5)), ("1224", (2024, 12))])
def test_mmyy(raw, expected):
    assert vinotek.parse_period(raw) == expected


def test_mmyy_rejects_an_impossible_month():
    assert vinotek.parse_period("9924") is None


def test_meeting_numbers():
    assert vinotek.parse_period("Møte 03 2023") == (2023, 3)
    assert vinotek.parse_period("Møte 11 2023") == (2023, 11)


def test_the_year_2323_is_read_as_2023():
    # 72 rows carry it, and correcting it merges a tasting otherwise split
    # between "Møte 03 2323" and "Møte 03 2023".
    assert vinotek.parse_period("Møte 03 2323") == vinotek.parse_period("Møte 03 2023")


def test_a_real_date_cell_discards_the_day():
    # Every date in the workbook is the 1st, so no day was ever recorded.
    assert vinotek.parse_period(datetime.datetime(2018, 2, 1)) == (2018, 2)


def test_a_month_with_no_year_needs_one_supplied():
    assert vinotek.parse_period("Oktober") is None
    assert vinotek.parse_period("Oktober", month_only_year=2021) == (2021, 10)
    assert vinotek.parse_period("Desember", month_only_year=2021) == (2021, 12)


@pytest.mark.parametrize("raw", ["Bonus: Ch. Panet 2018", "", "   ", None, 42, "Vin7"])
def test_unreadable_dates_are_refused_rather_than_guessed(raw):
    assert vinotek.parse_period(raw, month_only_year=2021) is None


def test_period_keys_sort_and_distinguish_a_missing_month():
    assert vinotek.Period(2021, 3).key() == "2021-03"
    assert vinotek.Period(2013, None).key() == "2013-00"
    assert vinotek.Period(2021, 3).key() < vinotek.Period(2021, 11).key()


# -- scores -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value,scale,expected",
    [
        (87, 1, 87),          # the 1-100 era, as typed
        (87.5, 1, 88),        # the seven fractional scores round
        (87.2, 1, 87),
        (9.5, 10, 95),        # the out-of-ten era, rescaled
        (10, 10, 100),
        (9.75, 10, 98),
        (8.7, 10, 87),
        (2, 10, 20),          # a genuinely harsh score, kept
    ],
)
def test_scores_land_on_one_scale(value, scale, expected):
    assert vinotek.normalise_score(value, scale=scale) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "n/a", 0, 0.0, -3])
def test_an_absent_or_zero_score_means_they_were_not_there(value):
    assert vinotek.normalise_score(value, scale=10) is None


def test_a_rescaled_score_cannot_escape_the_allowed_range():
    assert vinotek.normalise_score(11, scale=10) == 100
    assert vinotek.normalise_score(0.05, scale=10) == 1


# -- columns ----------------------------------------------------------------


def test_columns_are_found_by_name():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    assert (columns.tid, columns.theme, columns.wine) == (0, 2, 4)
    assert len(columns.members) == 9


def test_the_2015_price_column_shifts_everything_right():
    columns = vinotek.locate_columns(SHEET_2015)
    assert columns.price == 5
    assert columns.stated_average == 6
    assert min(columns.members.values()) == 7


def test_the_2013_sheet_has_no_date_column_and_a_tenth_member():
    columns = vinotek.locate_columns(SHEET_2013)
    assert columns.tid is None
    assert columns.wine == 3
    assert "Marius" in columns.members, "his scores are in the sheet's own Sum"
    assert columns.total == 15


def test_a_theme_header_overwritten_with_a_tasting_name_is_still_found():
    # The 2016 sheet titles its theme column "Top of the pops".
    columns = vinotek.locate_columns(SHEET_2016[:21])
    assert columns.theme == 2


def test_a_header_with_no_wine_column_is_an_error():
    with pytest.raises(ValueError, match="Vin"):
        vinotek.locate_columns(["Tid", "Ansvarlig", "Tema"])


def test_two_tables_side_by_side_are_both_found():
    # 2016 holds two, and reading only one loses half that year.
    blocks = vinotek.locate_blocks(SHEET_2016)
    assert len(blocks) == 2
    assert blocks[0].wine == 4
    assert blocks[1].wine == 26
    assert min(blocks[1].members.values()) == 28, "positions are whole-row, not block-relative"


def test_a_single_table_yields_one_block():
    assert len(vinotek.locate_blocks(ALLTIME_HEADER)) == 1
    assert len(vinotek.locate_blocks(SHEET_2013)) == 1


# -- rows -------------------------------------------------------------------


def row(**cells):
    values = [None] * len(ALLTIME_HEADER)
    for name, value in cells.items():
        column = {"tid": 0, "ansvarlig": 1, "tema": 2, "sted": 3, "vin": 4, "poeng": 5}[name]
        values[column] = value
    return values


def with_scores(scores, **cells):
    values = row(**cells)
    for name, score in scores.items():
        values[ALLTIME_HEADER.index(name)] = score
    return values


def test_a_row_becomes_a_wine():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, reason = vinotek.parse_row(
        with_scores(
            {"Andy": 87, "Tore": 90},
            tid="okt18", ansvarlig="Morten", tema="Rimelig Bordeaux", sted="Erk",
            vin="Ch. Simard 2000", poeng=88.5,
        ),
        columns, ALLTIME,
    )
    assert reason is None
    assert wine.period == (2018, 10)
    assert wine.name == "Ch. Simard 2000"
    assert wine.scores == {"Andy": 87, "Tore": 90}
    assert wine.brought_by == "Morten"
    assert wine.location == "Erk"


def test_a_non_breaking_space_in_a_name_is_normalised():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, _ = vinotek.parse_row(
        with_scores({"Andy": 87}, vin="Ch.\xa0Simard 2000"), columns, ALLTIME
    )
    assert wine.name == "Ch. Simard 2000"


def test_the_summary_row_is_not_a_wine():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, reason = vinotek.parse_row(
        with_scores({"Andy": 85, "Tore": 86}, vin="Snitt"), columns, ALLTIME
    )
    assert wine is None and reason == "summary row"


def test_a_row_with_no_wine_name_is_skipped():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, reason = vinotek.parse_row(with_scores({"Andy": 87}), columns, ALLTIME)
    assert wine is None and reason == "no wine name"


def test_a_subtotal_row_with_a_name_but_no_scores_is_skipped():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, reason = vinotek.parse_row(row(vin="Some wine", poeng=8.59), columns, ALLTIME)
    assert wine is None and reason == "no scores"


def test_the_early_era_is_rescaled_on_the_way_in():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    wine, _ = vinotek.parse_row(
        with_scores({"Andy": 9.5, "Tore": 8.7}, vin="Boroli Barolo 2005"), columns, EARLY
    )
    assert wine.scores == {"Andy": 95, "Tore": 87}


def test_a_sheet_year_dates_rows_that_have_no_date_column():
    columns = vinotek.locate_columns(SHEET_2013)
    values = [None] * len(SHEET_2013)
    values[3] = "Boroli Barolo 2005"
    values[SHEET_2013.index("Andy")] = 9
    spec = vinotek.SheetSpec("2013", 10, 2013)
    wine, _ = vinotek.parse_row(values, columns, spec)
    assert wine.period == (2013, None), "year known, month never recorded"


# -- identity ---------------------------------------------------------------


def test_a_tasting_is_identified_by_when_and_what():
    key = vinotek.tasting_key(vinotek.Period(2023, 3), "Women in Wine")
    assert key == vinotek.tasting_key(vinotek.Period(2023, 3), "women in wine")
    assert key != vinotek.tasting_key(vinotek.Period(2023, 4), "Women in Wine")


def test_the_location_is_deliberately_not_part_of_the_tasting_key():
    # On a bring-your-own night `Sted` records whose bottle each row is, so
    # keying on it would split one December evening into ten tastings.
    first = vinotek.tasting_key(vinotek.Period(2023, 12), "BYO")
    second = vinotek.tasting_key(vinotek.Period(2023, 12), "BYO")
    assert first == second


def test_three_themes_in_one_month_are_three_tastings():
    # May 2021 really did hold three.
    keys = {
        vinotek.tasting_key(vinotek.Period(2021, 5), theme)
        for theme in ("Verdt prisen?", "Parsell - Montmains", "Douro")
    }
    assert len(keys) == 3


def test_the_duplicate_signature_covers_the_name_and_every_score():
    columns = vinotek.locate_columns(ALLTIME_HEADER)
    make = lambda scores, spec: vinotek.parse_row(  # noqa: E731
        with_scores(scores, vin="Don Melchor 2007"), columns, spec
    )[0]
    # The same wine and scores recorded in two sheets, on the two scales.
    early = make({"Andy": 8.8, "Tore": 9.9}, EARLY)
    late = make({"Andy": 88, "Tore": 99}, ALLTIME)
    assert early.signature == late.signature

    # A genuine re-tasting scores differently, so it is not a duplicate.
    again = make({"Andy": 90, "Tore": 99}, ALLTIME)
    assert again.signature != late.signature


# -- undoing Excel's fill handle --------------------------------------------


def wine(period, theme, location, name):
    return vinotek.ParsedWine(
        period=period, theme=theme, location=location, brought_by=None,
        name=name, price_nok=None, scores={"Morten": 90}, stated_average=None,
        stated_total=None,
    )


def test_a_dragged_theme_year_collapses_to_the_typed_one():
    """The 2022 sheet's January evening reads 'Top of the pops 2021' … '2030'.

    Ten rows, one wine each, the year stepping by one: Excel extended the series
    instead of copying the cell. The evening is the one reviewing 2021.
    """
    rows = [
        wine(vinotek.Period(2022, 1), f"Top of the pops {2021 + i}", "Tore", f"Wine {i}")
        for i in range(10)
    ]
    repaired, notes = vinotek.undo_fill_handle(rows)
    assert {w.theme for w in repaired} == {"Top of the pops 2021"}
    assert len(notes) == 1 and "2021→2030" in notes[0]


def test_a_dragged_year_collapses_to_the_earliest():
    # The Riesling evening: the 2019 sheet has all eight rows at juli19, while
    # Alltime has them running juli19 → juli26.
    rows = [
        wine(vinotek.Period(2019 + i, 7), "Riesling", "Andy", f"Riesling {i}")
        for i in range(8)
    ]
    repaired, _ = vinotek.undo_fill_handle(rows)
    assert {w.period for w in repaired} == {vinotek.Period(2019, 7)}


def test_a_dragged_month_collapses_to_the_earliest():
    # 2018 Gevrey-Chambertin: February in the per-year sheet, February→September
    # in Alltime, with April missing because that row is not in Alltime.
    months = [2, 3, 5, 6, 7, 8, 9]
    rows = [wine(vinotek.Period(2018, m), "Gevrey-Chambertin", "Sigurd", f"W{m}") for m in months]
    repaired, notes = vinotek.undo_fill_handle(rows)
    assert {w.period for w in repaired} == {vinotek.Period(2018, 2)}
    assert "month" in notes[0]


def test_a_genuine_annual_evening_is_left_alone():
    """The club really does hold a 'Top of the pops' every January.

    Eight years of it, eight wines each — the years step by one, but no year is
    carried by a single row, so it is a series of evenings and not a drag.
    """
    rows = [
        wine(vinotek.Period(2015 + y, 1), "Top of the pops", "Tore", f"Wine {y}-{i}")
        for y in range(8)
        for i in range(8)
    ]
    repaired, notes = vinotek.undo_fill_handle(rows)
    assert notes == []
    assert len({w.period for w in repaired}) == 8


def test_two_holes_are_too_sparse_to_call_a_drag():
    months = [2, 4, 6, 8]  # a hole between each — not a dense run
    rows = [wine(vinotek.Period(2018, m), "Something", "Sigurd", f"W{m}") for m in months]
    _, notes = vinotek.undo_fill_handle(rows)
    assert notes == []


def test_a_short_run_is_left_alone():
    # Three consecutive months could easily be three real evenings.
    rows = [wine(vinotek.Period(2018, m), "Bring your own", "Sigurd", f"W{m}") for m in (2, 3, 4)]
    _, notes = vinotek.undo_fill_handle(rows)
    assert notes == []


def test_different_places_are_different_evenings():
    rows = [
        wine(vinotek.Period(2018, m), "Bring your own", f"Host{m}", f"W{m}")
        for m in range(2, 8)
    ]
    _, notes = vinotek.undo_fill_handle(rows)
    assert notes == [], "same theme at six different homes is six evenings"


def test_repair_never_moves_a_row_later():
    rows = [
        wine(vinotek.Period(2019 + i, 7), "Riesling", "Andy", f"R{i}") for i in range(8)
    ]
    repaired, _ = vinotek.undo_fill_handle(rows)
    assert all(w.period.year <= 2019 + i for i, w in enumerate(repaired))


def test_the_scores_are_never_touched():
    rows = [
        wine(vinotek.Period(2022, 1), f"Top of the pops {2021 + i}", "Tore", f"W{i}")
        for i in range(10)
    ]
    repaired, _ = vinotek.undo_fill_handle(rows)
    assert [w.scores for w in repaired] == [w.scores for w in rows]
    assert [w.name for w in repaired] == [w.name for w in rows]
