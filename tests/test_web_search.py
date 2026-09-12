"""Searching the wine archive.

The cases are real names out of the club's spreadsheet, so a regression here is
a regression against the actual archive.
"""

from __future__ import annotations

import pytest

from web.search import (
    ELSEWHERE,
    NAME_PREFIX,
    NAME_WORD,
    ORIGIN,
    Filters,
    Index,
    Row,
    tokenize,
)


def wine(id, name, **kw):
    return Row(
        id=id,
        name=name,
        country=kw.get("country"),
        region=kw.get("region"),
        grape=kw.get("grape"),
        vintage=kw.get("vintage"),
        theme=kw.get("theme"),
        year=kw.get("year"),
        brought_by=kw.get("brought_by"),
        average=kw.get("average"),
        ratings=kw.get("ratings", 0),
    )


ARCHIVE = [
    wine(1, "Boroli Barolo 2005", country="Italy", region="Barolo", grape="Nebbiolo",
         vintage=2005, theme="Piemonte", year=2013, average=81.8, ratings=9),
    wine(2, "Massolino Barolo 2015", country="Italy", region="Barolo", grape="Nebbiolo",
         vintage=2015, theme="Barolo", year=2019, average=87.3, ratings=9),
    wine(3, "Clusel Roch Cote Rotie 2007", country="France", region="Côte-Rôtie",
         grape="Syrah", vintage=2007, theme="Nord Rhône", year=2013, average=80.8, ratings=9),
    wine(4, "Stephane Ogier Côte-Rôtie Mon Village", country="France", region="Côte-Rôtie",
         grape="Syrah", theme="Top of the pops 2021", year=2022, average=91.5, ratings=8),
    wine(5, "Kanonkop Cabernet Sauvignon 2011 (Sør Afrika)", country="South Africa",
         grape="Cabernet Sauvignon", vintage=2011, theme="Sør Afrika", year=2015,
         average=83.4, ratings=8),
    wine(6, "Savage Thief in the Night", country="South Africa", theme="Sør Afrika",
         year=2015, average=87.5, ratings=8),
    wine(7, "Künstler Hochheim Spätburgunder 2015", country="Germany", grape="Pinot Noir",
         vintage=2015, theme="Tyskland", year=2018, average=82.8, ratings=9),
    wine(8, "Ch. Musar 2005", country="Lebanon", region="Bekaa Valley", vintage=2005,
         theme="Øst møter vest", year=2013, average=92.3, ratings=9),
    wine(9, "Vino Nobile Avignonesi 2010", country="Italy",
         region="Vino Nobile di Montepulciano", grape="Sangiovese", vintage=2010,
         theme="VM semifinalister", year=2014, average=64.8, ratings=9),
]


@pytest.fixture()
def index():
    return Index(ARCHIVE)


# -- folding, which is the whole point --------------------------------------


@pytest.mark.parametrize(
    "query,expected_id",
    [
        ("cote rotie", 3),        # the query has no accents
        ("côte-rôtie", 3),        # ...and the same query with them
        ("spatburgunder", 7),     # ä folded away
        ("kunstler", 7),
        ("sor afrika", 5),        # ø, which NFKD alone leaves intact
        ("Sør Afrika", 5),
    ],
)
def test_a_query_folds_the_same_way_the_index_did(index, query, expected_id):
    assert expected_id in {h.id for h in index.search(query)}


def test_norwegian_letters_in_a_theme_are_searchable(index):
    # "Øst møter vest" — the evening, not the wine.
    assert {h.id for h in index.search("ost moter vest")} == {8}


# -- ranking ----------------------------------------------------------------


def test_a_wine_named_barolo_beats_one_merely_poured_at_a_barolo_evening(index):
    hits = index.search("barolo")
    assert {h.id for h in hits} == {1, 2}
    assert all(h.band <= NAME_WORD for h in hits), "both are named Barolo"


def test_the_theme_still_matches_when_the_name_does_not(index):
    # Savage Thief in the Night says nothing about South Africa; the evening does.
    hits = {h.id: h.band for h in index.search("sor afrika")}
    assert hits[5] < hits[6], "the wine with it in its name ranks first"
    assert hits[6] == ELSEWHERE


def test_a_name_prefix_outranks_a_name_word():
    starts = wine(60, "Barolo Marchesi di Barolo", average=88.0, ratings=9)
    contains = wine(61, "Massolino Barolo 2015", average=95.0, ratings=9)
    hits = Index([contains, starts]).search("barolo")
    assert [h.id for h in hits] == [60, 61], "the prefix wins despite the lower average"
    assert [h.band for h in hits] == [NAME_PREFIX, NAME_WORD]


def test_the_worst_placed_token_decides_the_band(index):
    """A wine is only a name match when the *whole* query is in its name."""
    # "barolo" is in the name, "piemonte" only in the theme.
    (hit,) = index.search("barolo piemonte")
    assert hit.id == 1
    assert hit.band == ELSEWHERE


def test_origin_fields_rank_above_the_evening(index):
    # "nebbiolo" is the grape, which is origin, not just anywhere.
    hits = index.search("nebbiolo")
    assert {h.band for h in hits} == {ORIGIN}


def test_ties_go_to_the_wine_more_people_rated(index):
    a = wine(20, "Tie One", average=90.0, ratings=9)
    b = wine(21, "Tie Two", average=90.0, ratings=3)
    assert [h.id for h in Index([b, a]).search("tie")] == [20, 21]


def test_ordering_is_deterministic():
    a = wine(30, "Same Same B", average=90.0, ratings=9)
    b = wine(31, "Same Same A", average=90.0, ratings=9)
    assert [h.id for h in Index([a, b]).search("same")] == [31, 30]


# -- every token must match -------------------------------------------------


def test_all_tokens_must_be_present(index):
    assert index.search("barolo 2005")  # both true of Boroli
    assert not index.search("barolo bordeaux")


def test_a_quoted_phrase_stays_whole(index):
    assert {h.id for h in index.search('"vino nobile"')} == {9}
    # Without the quotes, a wine holding both words apart would also match.
    loose = Index([*ARCHIVE, wine(40, "Nobile Something", theme="Vino Rosso")])
    assert 40 in {h.id for h in loose.search("vino nobile")}
    assert 40 not in {h.id for h in loose.search('"vino nobile"')}


def test_nothing_matching_is_empty_not_an_error(index):
    assert index.search("zzzznothing") == []


def test_an_empty_query_returns_the_archive(index):
    assert len(index.search("", limit=None)) == len(ARCHIVE)


def test_an_empty_index_searches_fine():
    assert Index([]).search("barolo") == []


@pytest.mark.parametrize("query", ["", "   ", '""', "  \t "])
def test_blank_queries_do_not_crash(index, query):
    assert len(index.search(query, limit=None)) == len(ARCHIVE)


def test_tokenize_keeps_phrases_and_drops_empties():
    assert tokenize('"vino nobile" barolo') == ["vino nobile", "barolo"]
    assert tokenize("  ") == []
    assert tokenize('""') == []


# -- filters ----------------------------------------------------------------


def test_a_filter_narrows_without_a_query(index):
    hits = index.search("", Filters(country="Italy"), limit=None)
    assert {h.id for h in hits} == {1, 2, 9}


def test_filters_and_query_combine(index):
    hits = index.search("barolo", Filters(vintage_from=2010))
    assert {h.id for h in hits} == {2}


def test_a_missing_value_never_satisfies_a_range(index):
    # Wine 4 has no vintage; a vintage filter must exclude it rather than guess.
    hits = index.search("cote rotie", Filters(vintage_from=2000, vintage_to=2030))
    assert {h.id for h in hits} == {3}


def test_year_range_uses_the_tasting_not_the_vintage(index):
    hits = index.search("", Filters(year_from=2019, year_to=2022), limit=None)
    assert {h.id for h in hits} == {2, 4}


def test_min_score_excludes_the_unrated(index):
    unrated = wine(50, "Never Scored", country="Italy")
    hits = Index([*ARCHIVE, unrated]).search("", Filters(min_score=80.0), limit=None)
    assert 50 not in {h.id for h in hits}
    assert 9 not in {h.id for h in hits}, "Avignonesi averages 64.8"


def test_empty_filters_report_themselves_empty():
    assert Filters().empty
    assert not Filters(country="Italy").empty


# -- facets -----------------------------------------------------------------


def test_facets_count_and_sort_by_commonest(index):
    assert index.facets("country")[0] == ("Italy", 3)


def test_facets_skip_the_unplaced(index):
    assert all(name for name, _ in index.facets("grape"))
    assert dict(index.facets("grape"))["Nebbiolo"] == 2


# -- limits -----------------------------------------------------------------


def test_limit_caps_the_result(index):
    assert len(index.search("", limit=2)) == 2


def test_no_limit_returns_everything(index):
    assert len(index.search("", limit=None)) == len(ARCHIVE)


def test_a_multi_word_query_is_judged_by_its_worst_placed_word():
    """"vino nobile" against a wine named "Vino Nobile Avignonesi".

    "vino" starts the name, "nobile" sits inside it, so the pair lands on the
    weaker of the two bands. Anything else would let one lucky word promote a
    query that only half fits.
    """
    (hit,) = Index([ARCHIVE[8]]).search("vino nobile")
    assert hit.band == NAME_WORD
