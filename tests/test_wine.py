"""The wine maths: averages, value-for-money, and the rating bar."""

from __future__ import annotations

import pytest

from bot.cogs.wine import group_average, rating_bar, value_score


def test_group_average_of_nothing_is_none():
    assert group_average([]) is None


@pytest.mark.parametrize(
    "scores,expected",
    [([90], 90.0), ([90, 94], 92.0), ([80, 85, 90], 85.0)],
)
def test_group_average(scores, expected):
    assert group_average(scores) == pytest.approx(expected)


def test_value_score_is_points_per_hundred_kroner():
    # 92 points at 200 kr is 46 points per 100 kr.
    assert value_score(92, 200) == pytest.approx(46.0)


def test_cheaper_bottle_with_same_score_wins_on_value():
    assert value_score(90, 150) > value_score(90, 600)


def test_better_bottle_at_same_price_wins_on_value():
    assert value_score(95, 300) > value_score(85, 300)


@pytest.mark.parametrize("average,price", [(None, 300), (92, None), (92, 0), (92, -10)])
def test_value_score_needs_both_a_rating_and_a_real_price(average, price):
    assert value_score(average, price) is None


def test_rating_bar_scales_and_keeps_a_fixed_width():
    assert rating_bar(100, width=10) == "█" * 10
    assert rating_bar(1, width=10).count("█") == 0
    assert len(rating_bar(57, width=10)) == 10
    assert rating_bar(50, width=10) == "█████░░░░░"
