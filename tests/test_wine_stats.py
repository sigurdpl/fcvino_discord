"""Rolling the cellar up by country, region and grape."""

from __future__ import annotations

import pytest

from bot import wine_stats
from bot.wine_stats import Tally, coverage, spread, tally


def rating(key, wine_id, score, tasting_id=None):
    """One rating. Distinct tasting per wine by default, so evening-counting
    only kicks in where a test says so."""
    return {
        "key": key,
        "wine_id": wine_id,
        "score": score,
        "tasting_id": tasting_id if tasting_id is not None else 1000 + wine_id,
    }


def test_a_bucket_averages_every_rating_not_every_wine():
    # Two bottles: one rated by four people at 90, one by a single person at 50.
    # Mean-of-wines would say 70; the club's actual view is much closer to 90.
    rows = [rating("Italy", 1, 90) for _ in range(4)] + [rating("Italy", 2, 50)]
    (italy,) = tally(rows, minimum=2)
    assert italy.average == pytest.approx(82.0)
    assert (italy.wines, italy.ratings) == (2, 5)


def test_a_bucket_below_the_minimum_is_left_off():
    rows = [
        rating("Georgia", 1, 99),
        *[rating("Italy", i, 80) for i in range(2, 7)],
    ]
    names = [t.name for t in tally(rows, minimum=5)]
    assert names == ["Italy"], "one brilliant bottle is not a country's record"


def test_the_minimum_counts_bottles_not_ratings():
    # Nine of us rating the same bottle is still one bottle.
    rows = [rating("Georgia", 1, 99) for _ in range(9)]
    assert tally(rows, minimum=5) == []


def test_best_average_first():
    rows = [
        *[rating("Italy", i, 85) for i in range(1, 6)],
        *[rating("Spain", i, 79) for i in range(6, 11)],
        *[rating("France", i, 91) for i in range(11, 16)],
    ]
    assert [t.name for t in tally(rows)] == ["France", "Italy", "Spain"]


def test_bottle_count_breaks_a_tie():
    rows = [
        *[rating("Italy", i, 85) for i in range(1, 9)],
        *[rating("Spain", i, 85) for i in range(9, 14)],
    ]
    assert [t.name for t in tally(rows)] == ["Italy", "Spain"]


def test_an_unplaced_wine_is_on_no_board():
    rows = [rating(None, 1, 95), *[rating("Italy", i, 80) for i in range(2, 7)]]
    assert [t.name for t in tally(rows)] == ["Italy"]
    assert sum(t.ratings for t in tally(rows)) == 5


def test_an_empty_cellar_ranks_nothing():
    assert tally([]) == []


# -- coverage ---------------------------------------------------------------


def test_coverage_counts_filled_columns():
    rows = [
        {"country": "Italy", "grape": None},
        {"country": "France", "grape": "Syrah"},
        {"country": None, "grape": "Syrah"},
        {"country": None, "grape": None},
    ]
    country, grape = coverage(rows, ["country", "grape"])
    assert (country.known, country.total) == (2, 4)
    assert country.share == pytest.approx(0.5)
    assert grape.known == 2


def test_coverage_of_nothing_is_not_a_division_by_zero():
    (c,) = coverage([], ["country"])
    assert (c.known, c.total, c.share) == (0, 0, 0.0)


def test_a_zero_is_present_not_missing():
    # `is not None`, not truthiness — a vintage of 0 would be odd, but a score
    # of 0 in some other column must not read as "never filled in".
    (c,) = coverage([{"vintage": 0}], ["vintage"])
    assert c.known == 1


# -- spread -----------------------------------------------------------------


def test_spread_is_the_gap_across_the_board():
    board = [Tally("France", 9, 40, 4, 88.0), Tally("Chile", 5, 20, 3, 79.5)]
    assert spread(board) == pytest.approx(8.5)


def test_spread_needs_two_to_compare():
    assert spread([Tally("France", 9, 40, 4, 88.0)]) is None
    assert spread([]) is None


def test_the_default_minimum_is_the_documented_one():
    assert wine_stats.MIN_WINES == 5


# -- one evening, or a real verdict? -----------------------------------------


def test_bottles_from_one_night_are_marked():
    # The cellar's five Dolcettos are all from a single tasting and come last.
    # That may be a verdict on Dolcetto or on the evening; the board must not
    # silently claim the former.
    rows = [rating("Dolcetto", i, 57, tasting_id=7) for i in range(1, 6)]
    (dolcetto,) = tally(rows)
    assert dolcetto.tastings == 1
    assert dolcetto.one_evening


def test_bottles_spread_over_evenings_are_not():
    rows = [rating("Barolo", i, 87, tasting_id=i % 3) for i in range(1, 8)]
    (barolo,) = tally(rows)
    assert barolo.tastings == 3
    assert not barolo.one_evening


def test_a_bottle_outside_any_tasting_stands_alone():
    # Five loose bottles are five separate occasions, not one shared evening.
    rows = [rating("Riesling", i, 80, tasting_id=None) for i in range(1, 6)]
    (riesling,) = tally(rows)
    assert riesling.tastings == 5
    assert not riesling.one_evening


def test_loose_bottles_do_not_collapse_into_one_night():
    rows = [
        *[rating("Riesling", i, 80, tasting_id=None) for i in range(1, 4)],
        *[rating("Riesling", i, 80, tasting_id=4) for i in range(4, 6)],
    ]
    (riesling,) = tally(rows)
    assert riesling.tastings == 4, "three loose bottles plus one shared evening"
