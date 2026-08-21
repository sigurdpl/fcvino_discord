"""The trips archive's arithmetic, on rows built by hand.

These are the functions behind "how many countries have we seen", so they are
worth pinning down precisely — including the cases where a trip is recorded but
nobody remembers the score.
"""

from __future__ import annotations

import pytest

from bot import trip_stats as stats


def match(
    year,
    home,
    away,
    home_goals=None,
    away_goals=None,
    *,
    stadium=None,
    competition=None,
    attendance=None,
    match_date=None,
    country="Germany",
):
    return {
        "id": year,
        "trip_id": year,
        "year": year,
        "country": country,
        "trip_city": None,
        "home": home,
        "away": away,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "stadium": stadium,
        "competition": competition,
        "attendance": attendance,
        "match_date": match_date,
        "notes": None,
    }


def trip(year, country, city=None):
    return {"year": year, "country": country, "city": city}


SEASON = [
    match(2014, "Dortmund", "Bayern", 0, 3, stadium="Signal Iduna Park", competition="Bundesliga"),
    match(2015, "Arsenal", "Chelsea", 0, 0, stadium="Emirates", competition="Premier League"),
    match(2016, "Roma", "Lazio", 4, 3, stadium="Olimpico", competition="Serie A"),
    match(2017, "Ajax", "Feyenoord", 2, 1, stadium="Johan Cruijff Arena"),
]


# -- filtering --------------------------------------------------------------


def test_played_excludes_matches_with_no_score():
    rows = [*SEASON, match(2018, "Celtic", "Rangers")]
    assert len(stats.played(rows)) == 4


def test_a_scoreless_match_still_counts_as_a_match_attended():
    # It has no score, so it cannot contribute goals — but we were there.
    rows = [match(2018, "Celtic", "Rangers")]
    assert stats.total_goals(rows) == 0
    assert stats.goals_per_game(rows) is None


# -- countries and grounds --------------------------------------------------


def test_country_counts_orders_by_visits_then_alphabetically():
    trips = [
        trip(2010, "England"),
        trip(2011, "Germany"),
        trip(2012, "England"),
        trip(2013, "Austria"),
    ]
    assert stats.country_counts(trips) == [("England", 2), ("Austria", 1), ("Germany", 1)]


def test_country_counts_of_nothing_is_empty():
    assert stats.country_counts([]) == []


def test_years_are_sorted_and_deduplicated():
    trips = [trip(2012, "A"), trip(2010, "B"), trip(2012, "C")]
    assert stats.years(trips) == [2010, 2012]


def test_distinct_stadiums_ignores_blanks_and_duplicates():
    rows = [
        match(2014, "a", "b", stadium="Emirates"),
        match(2015, "c", "d", stadium="Emirates"),
        match(2016, "e", "f", stadium=None),
    ]
    assert stats.distinct_stadiums(rows) == ["Emirates"]


def test_total_attendance_sums_what_is_known_and_is_none_when_nothing_is():
    rows = [
        match(2014, "a", "b", attendance=80667),
        match(2015, "c", "d", attendance=60000),
        match(2016, "e", "f"),
    ]
    assert stats.total_attendance(rows) == 140667
    assert stats.total_attendance([match(2016, "e", "f")]) is None


# -- goals and results ------------------------------------------------------


def test_total_goals_and_average():
    assert stats.total_goals(SEASON) == 13
    assert stats.goals_per_game(SEASON) == pytest.approx(13 / 4)


def test_result_split_counts_home_draw_away():
    assert stats.result_split(SEASON) == (2, 1, 1)


def test_biggest_win_prefers_the_widest_margin():
    assert stats.biggest_win(SEASON)["home"] == "Dortmund"  # 0-3, margin 3


def test_biggest_win_breaks_a_margin_tie_on_total_goals():
    rows = [match(2019, "A", "B", 1, 0), match(2020, "C", "D", 3, 2)]
    # Both margin 1; the 5-goal game is the better story.
    assert stats.biggest_win(rows)["home"] == "C"


def test_biggest_win_ignores_draws_entirely():
    assert stats.biggest_win([match(2019, "A", "B", 2, 2)]) is None


def test_highest_scoring_counts_both_sides():
    assert stats.highest_scoring(SEASON)["home"] == "Roma"  # 4-3 = 7


def test_goalless_finds_the_nil_nils():
    assert [m["home"] for m in stats.goalless(SEASON)] == ["Arsenal"]


def test_goals_by_year_is_ranked_and_includes_a_goalless_year():
    assert stats.goals_by_year(SEASON) == [(2016, 7), (2014, 3), (2017, 3), (2015, 0)]


def test_goal_functions_survive_an_empty_archive():
    assert stats.total_goals([]) == 0
    assert stats.goals_per_game([]) is None
    assert stats.result_split([]) == (0, 0, 0)
    assert stats.biggest_win([]) is None
    assert stats.highest_scoring([]) is None
    assert stats.goals_by_year([]) == []


# -- clubs and competitions -------------------------------------------------


def test_club_counts_counts_home_and_away_appearances():
    rows = [match(2014, "Arsenal", "Chelsea", 1, 0), match(2015, "Chelsea", "Spurs", 2, 2)]
    assert stats.club_counts(rows) == [("Chelsea", 2), ("Arsenal", 1), ("Spurs", 1)]


def test_repeat_clubs_only_lists_the_ones_seen_twice():
    rows = [match(2014, "Arsenal", "Chelsea", 1, 0), match(2015, "Chelsea", "Spurs", 2, 2)]
    assert stats.repeat_clubs(rows) == [("Chelsea", 2)]


def test_competition_counts_skips_matches_with_no_competition():
    assert stats.competition_counts(SEASON) == [
        ("Bundesliga", 1),
        ("Premier League", 1),
        ("Serie A", 1),
    ]


def test_top_scorers_ranks_players_and_respects_the_limit():
    goals = [
        {"scorer": "Robben"},
        {"scorer": "Robben"},
        {"scorer": "Mueller"},
        {"scorer": None},
    ]
    assert stats.top_scorers(goals) == [("Robben", 2), ("Mueller", 1)]
    assert stats.top_scorers(goals, limit=1) == [("Robben", 2)]


# -- streaks ----------------------------------------------------------------


def test_longest_streak_finds_the_longest_run():
    assert stats.longest_year_streak([2010, 2011, 2012, 2014, 2015]) == (2010, 2012)


def test_longest_streak_prefers_the_earliest_of_two_equal_runs():
    assert stats.longest_year_streak([2010, 2011, 2013, 2014]) == (2010, 2011)


def test_longest_streak_handles_a_later_longer_run():
    assert stats.longest_year_streak([2010, 2011, 2015, 2016, 2017]) == (2015, 2017)


def test_a_single_year_is_a_streak_of_one():
    assert stats.longest_year_streak([2019]) == (2019, 2019)


def test_no_years_has_no_streak():
    assert stats.longest_year_streak([]) is None


def test_streak_ignores_duplicate_years():
    assert stats.longest_year_streak([2010, 2010, 2011]) == (2010, 2011)


# -- missing detail ---------------------------------------------------------


def test_missing_detail_lists_every_empty_field():
    assert stats.missing_detail(match(2018, "Celtic", "Rangers")) == [
        "date",
        "competition",
        "score",
        "stadium",
        "attendance",
    ]


def test_missing_detail_is_empty_for_a_complete_match():
    complete = match(
        2014,
        "Dortmund",
        "Bayern",
        0,
        3,
        stadium="Signal Iduna Park",
        competition="Bundesliga",
        attendance=80667,
        match_date="2014-04-26",
    )
    assert stats.missing_detail(complete) == []


def test_a_nil_nil_is_a_recorded_score_not_a_missing_one():
    row = match(2015, "Arsenal", "Chelsea", 0, 0)
    assert "score" not in stats.missing_detail(row)


# -- parsing ----------------------------------------------------------------


def test_parse_goals_reads_minute_scorer_and_side():
    goals, errors = stats.parse_goals("23 Haaland H, 81 Kane A")
    assert errors == []
    assert goals == [(23, "Haaland", "H"), (81, "Kane", "A")]


def test_parse_goals_accepts_stoppage_time_and_the_minute_tick():
    goals, _ = stats.parse_goals("45+2 Foden (H); 90' Son A")
    assert [(g.minute, g.scorer, g.side) for g in goals] == [
        (45, "Foden", "H"),
        (90, "Son", "A"),
    ]


def test_parse_goals_allows_a_missing_side():
    goals, errors = stats.parse_goals("12 Odegaard")
    assert errors == [] and goals[0].side is None


def test_parse_goals_keeps_the_good_and_returns_the_bad():
    goals, errors = stats.parse_goals("23 Haaland H, nonsense, 81 Kane A")
    assert len(goals) == 2
    assert errors == ["nonsense"]


def test_parse_goals_handles_multi_word_names():
    goals, _ = stats.parse_goals("55 Van Dijk H")
    assert goals[0].scorer == "Van Dijk"


def test_parse_goals_of_nothing_is_nothing():
    assert stats.parse_goals("") == ([], [])
    assert stats.parse_goals("  ,  ; ") == ([], [])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  england ", "England"),
        ("GERMANY", "Germany"),
        ("czech REPUBLIC", "Czech Republic"),
        ("bosnia and herzegovina", "Bosnia and Herzegovina"),
        ("USA", "USA"),
        ("", ""),
    ],
)
def test_normalise_country(raw, expected):
    assert stats.normalise_country(raw) == expected


def test_normalisation_is_what_stops_england_being_counted_twice():
    trips = [
        trip(2010, stats.normalise_country("england")),
        trip(2012, stats.normalise_country("England ")),
    ]
    assert stats.country_counts(trips) == [("England", 2)]
