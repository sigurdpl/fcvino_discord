"""The cellar's database side: search, resolution, and the two boards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.cogs.wine import MIN_RATINGS_FOR_BOARD, Wine
from bot.db import utcnow_iso


@pytest.fixture()
def cog(db):
    instance = Wine.__new__(Wine)
    instance.bot = SimpleNamespace(db=db)
    return instance


def add_wine(db, name, *, producer=None, price=None, country=None, grape=None, vintage=None):
    return db.execute(
        """INSERT INTO wines
               (name, producer, vintage, country, grape, price_nok, added_by, added_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (name, producer, vintage, country, grape, price, utcnow_iso()),
    )


def rate(db, wine_id, user_id, score, notes=None):
    db.execute(
        """INSERT INTO wine_ratings (wine_id, user_id, score, notes, rated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (wine_id, user_id, score, notes, utcnow_iso()),
    )


def test_search_matches_producer_country_and_grape(cog, db):
    add_wine(db, "Barolo", producer="Vietti", country="Italy", grape="Nebbiolo")
    assert [r["name"] for r in cog._search_rows("viett")] == ["Barolo"]
    assert [r["name"] for r in cog._search_rows("ital")] == ["Barolo"]
    assert [r["name"] for r in cog._search_rows("nebbio")] == ["Barolo"]
    assert cog._search_rows("rioja") == []


def test_resolve_accepts_an_id_as_the_autocomplete_sends_it(cog, db):
    wine_id = add_wine(db, "Barolo")
    assert cog._resolve(str(wine_id))["id"] == wine_id


def test_resolve_falls_back_to_a_name_match_when_typed_freehand(cog, db):
    wine_id = add_wine(db, "Chablis Premier Cru")
    assert cog._resolve("chablis")["id"] == wine_id


def test_resolve_returns_none_for_a_nonexistent_id(cog, db):
    assert cog._resolve("9999") is None


def test_top_board_needs_the_minimum_number_of_ratings(cog, db):
    lonely = add_wine(db, "Solo")
    rate(db, lonely, 1, 99)
    assert cog._top_rows(10) == []

    popular = add_wine(db, "Shared")
    rate(db, popular, 1, 80)
    rate(db, popular, 2, 84)
    assert [r["name"] for r in cog._top_rows(10)] == ["Shared"]
    assert MIN_RATINGS_FOR_BOARD == 2


def test_top_board_orders_by_group_average(cog, db):
    good = add_wine(db, "Good")
    better = add_wine(db, "Better")
    for wine_id, scores in ((good, (80, 82)), (better, (90, 94))):
        for user_id, score in enumerate(scores, start=1):
            rate(db, wine_id, user_id, score)
    assert [r["name"] for r in cog._top_rows(10)] == ["Better", "Good"]


def test_top_board_respects_the_limit(cog, db):
    for i in range(4):
        wine_id = add_wine(db, f"Wine {i}")
        rate(db, wine_id, 1, 80 + i)
        rate(db, wine_id, 2, 80 + i)
    assert len(cog._top_rows(2)) == 2


def test_value_board_prefers_the_bargain(cog, db):
    cheap = add_wine(db, "Cheap", price=150)
    dear = add_wine(db, "Dear", price=900)
    for wine_id in (cheap, dear):
        rate(db, wine_id, 1, 90)
        rate(db, wine_id, 2, 90)
    assert [r["name"] for r in cog._value_rows(10)] == ["Cheap", "Dear"]


def test_value_board_skips_bottles_without_a_usable_price(cog, db):
    unpriced = add_wine(db, "Unpriced")
    free = add_wine(db, "Free", price=0)
    for wine_id in (unpriced, free):
        rate(db, wine_id, 1, 95)
        rate(db, wine_id, 2, 95)
    assert cog._value_rows(10) == []


def test_rating_rows_are_ordered_by_score(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, 1, 88)
    rate(db, wine_id, 2, 95)
    assert [r["score"] for r in cog._rating_rows(wine_id)] == [95, 88]


def test_rerating_replaces_the_previous_score_rather_than_adding_one(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, 1, 88)
    db.execute(
        """INSERT INTO wine_ratings (wine_id, user_id, score, notes, rated_at)
           VALUES (?, 1, 93, 'better second time', ?)
           ON CONFLICT(wine_id, user_id) DO UPDATE SET
               score=excluded.score, notes=excluded.notes, rated_at=excluded.rated_at""",
        (wine_id, utcnow_iso()),
    )
    rows = cog._rating_rows(wine_id)
    assert len(rows) == 1 and rows[0]["score"] == 93
