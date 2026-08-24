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


def member(db, name):
    """A rater. Ratings hang off wine_members, not off a Discord id."""
    db.execute(
        "INSERT INTO wine_members (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,)
    )
    return db.query_one("SELECT id FROM wine_members WHERE name=?", (name,))["id"]


def rate(db, wine_id, who, score, notes=None):
    member_id = member(db, who) if isinstance(who, str) else who
    db.execute(
        """INSERT INTO wine_ratings (wine_id, member_id, score, notes, rated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(wine_id, member_id) DO UPDATE SET
               score=excluded.score, notes=excluded.notes""",
        (wine_id, member_id, score, notes, utcnow_iso()),
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
    rate(db, lonely, "Andy", 99)
    assert cog._top_rows(10) == []

    popular = add_wine(db, "Shared")
    rate(db, popular, "Andy", 80)
    rate(db, popular, "Tore", 84)
    assert [r["name"] for r in cog._top_rows(10)] == ["Shared"]
    assert MIN_RATINGS_FOR_BOARD == 2


def test_top_board_orders_by_group_average(cog, db):
    good = add_wine(db, "Good")
    better = add_wine(db, "Better")
    for wine_id, scores in ((good, (80, 82)), (better, (90, 94))):
        for user_id, score in enumerate(scores, start=1):
            rate(db, wine_id, f"m{user_id}", score)
    assert [r["name"] for r in cog._top_rows(10)] == ["Better", "Good"]


def test_top_board_respects_the_limit(cog, db):
    for i in range(4):
        wine_id = add_wine(db, f"Wine {i}")
        rate(db, wine_id, "Andy", 80 + i)
        rate(db, wine_id, "Tore", 80 + i)
    assert len(cog._top_rows(2)) == 2


def test_value_board_prefers_the_bargain(cog, db):
    cheap = add_wine(db, "Cheap", price=150)
    dear = add_wine(db, "Dear", price=900)
    for wine_id in (cheap, dear):
        rate(db, wine_id, "Andy", 90)
        rate(db, wine_id, "Tore", 90)
    assert [r["name"] for r in cog._value_rows(10)] == ["Cheap", "Dear"]


def test_value_board_skips_bottles_without_a_usable_price(cog, db):
    unpriced = add_wine(db, "Unpriced")
    free = add_wine(db, "Free", price=0)
    for wine_id in (unpriced, free):
        rate(db, wine_id, "Andy", 95)
        rate(db, wine_id, "Tore", 95)
    assert cog._value_rows(10) == []


def test_rating_rows_are_ordered_by_score(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, "Andy", 88)
    rate(db, wine_id, "Tore", 95)
    assert [r["score"] for r in cog._rating_rows(wine_id)] == [95, 88]


def test_rerating_replaces_the_previous_score_rather_than_adding_one(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, "Andy", 88)
    rate(db, wine_id, "Andy", 93, "better second time")
    rows = cog._rating_rows(wine_id)
    assert len(rows) == 1 and rows[0]["score"] == 93


# -- members: the spreadsheet knows names, Discord knows accounts -------------


def user(user_id=500, display_name="someone"):
    return SimpleNamespace(id=user_id, display_name=display_name)


def test_a_member_from_the_sheet_is_reused_once_claimed(cog, db):
    member_id = member(db, "Sigurd")
    db.execute("UPDATE wine_members SET discord_id=? WHERE id=?", (500, member_id))
    assert cog._member_for(user(500)) == member_id, "no second row for the same person"


def test_someone_not_in_the_sheet_gets_their_own_row(cog, db):
    created = cog._member_for(user(999, "newcomer"))
    row = db.query_one("SELECT * FROM wine_members WHERE id=?", (created,))
    assert row["name"] == "newcomer"
    assert row["discord_id"] == 999


def test_member_lookup_is_stable_across_calls(cog, db):
    first = cog._member_for(user(999, "newcomer"))
    second = cog._member_for(user(999, "newcomer"))
    assert first == second
    assert db.query_one("SELECT COUNT(*) AS n FROM wine_members")["n"] == 1


def test_a_rater_is_named_until_they_claim_the_account(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, "Håvard", 92)
    row = cog._rating_rows(wine_id)[0]
    assert cog._rater(row) == "**Håvard**"

    db.execute("UPDATE wine_members SET discord_id=777 WHERE name='Håvard'")
    row = cog._rating_rows(wine_id)[0]
    assert cog._rater(row) == "<@777>", "a mention once the name is claimed"


def test_rating_rows_carry_the_name_and_the_account(cog, db):
    wine_id = add_wine(db, "Barolo")
    rate(db, wine_id, "Tore", 88)
    row = cog._rating_rows(wine_id)[0]
    assert row["member_name"] == "Tore"
    assert row["discord_id"] is None


def test_a_wine_knows_which_tasting_it_came_from(cog, db):
    db.execute(
        """INSERT INTO tastings (key, year, month, theme, location, host, added_at)
           VALUES ('2015-11|nord rhône', 2015, 11, 'Nord Rhône', 'Morten', 'Morten', 'x')"""
    )
    tasting_id = db.query_one("SELECT id FROM tastings")["id"]
    wine_id = add_wine(db, "Clusel Roch Côte-Rôtie 2007")
    db.execute("UPDATE wines SET tasting_id=? WHERE id=?", (tasting_id, wine_id))

    tasting = cog._tasting_for(wine_id)
    assert (tasting["year"], tasting["month"], tasting["theme"]) == (2015, 11, "Nord Rhône")


def test_a_wine_added_outside_a_tasting_has_none(cog, db):
    assert cog._tasting_for(add_wine(db, "Barolo")) is None
