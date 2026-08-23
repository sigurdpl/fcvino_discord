"""The trips commands driven end to end, with Discord stubbed out.

Calls the real command callbacks — the same code Discord invokes — so the whole
path is covered: validation, writes, and the embed that comes back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.cogs.trips import Trips


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    @property
    def last(self) -> tuple[str | None, dict]:
        return self.messages[-1]

    @property
    def last_embed(self):
        return self.last[1].get("embed")

    def text(self) -> str:
        """Everything the user would see, message and embed alike."""
        content, kwargs = self.last
        parts = [content or ""]
        e = kwargs.get("embed")
        if e is not None:
            parts += [e.title or "", e.description or ""]
            parts += [f"{f.name} {f.value}" for f in e.fields]
        return "\n".join(parts)


class FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.response = FakeResponse()
        self.user = SimpleNamespace(id=user_id)
        self.guild = None
        self.channel = None


@pytest.fixture()
def cog(db):
    instance = Trips.__new__(Trips)
    instance.bot = SimpleNamespace(db=db)
    return instance


def call(command, cog, interaction, **kwargs):
    asyncio.run(command.callback(cog, interaction, **kwargs))
    return interaction.response


# -- adding a trip ----------------------------------------------------------


def test_add_records_a_trip_and_reports_back(cog, db):
    response = call(
        Trips.add, cog, FakeInteraction(),
        year=2014, country="germany", city="Dortmund", date_from="2014-04-25",
        date_to="2014-04-27", notes="delayed flight",
    )
    assert "Recorded" in response.text()
    trip = db.query_one("SELECT * FROM trips")
    assert (trip["year"], trip["country"], trip["city"]) == (2014, "Germany", "Dortmund")
    assert trip["notes"] == "delayed flight"


def test_adding_the_same_year_again_amends_rather_than_duplicates(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany", notes="keep me")
    response = call(
        Trips.add, cog, FakeInteraction(), year=2014, country="Germany", city="Dortmund"
    )
    assert "Updated" in response.text()
    assert db.query_one("SELECT COUNT(*) AS n FROM trips")["n"] == 1
    trip = db.query_one("SELECT * FROM trips")
    assert (trip["city"], trip["notes"]) == ("Dortmund", "keep me")


def test_add_rejects_an_unparseable_date_without_writing(cog, db):
    response = call(
        Trips.add, cog, FakeInteraction(), year=2014, country="Germany", date_from="25/04/2014"
    )
    assert "YYYY-MM-DD" in response.text()
    assert db.query("SELECT * FROM trips") == []


def test_add_rejects_a_trip_that_ends_before_it_starts(cog, db):
    response = call(
        Trips.add, cog, FakeInteraction(), year=2014, country="Germany",
        date_from="2014-04-27", date_to="2014-04-25",
    )
    assert "before the first day" in response.text()
    assert db.query("SELECT * FROM trips") == []


# -- adding a match ---------------------------------------------------------


def test_add_match_attaches_to_the_trip_and_flags_what_is_missing(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany")
    response = call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=0, away_goals=3,
    )
    text = response.text()
    assert "Dortmund **0–3** Bayern" in text
    assert "Still missing" in text and "stadium" in text
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_matches")["n"] == 1


def test_add_match_says_nothing_is_missing_when_it_is_complete(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany")
    response = call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=0, away_goals=3,
        match_date="2014-04-26", competition="Bundesliga", city="Dortmund",
        stadium="Signal Iduna Park", attendance=80667,
    )
    assert "Still missing" not in response.text()


def test_a_match_inherits_the_trips_city_rather_than_reporting_it_missing(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2015, country="England", city="London")
    response = call(
        Trips.add_match, cog, FakeInteraction(),
        year=2015, home="Arsenal", away="Chelsea", home_goals=1, away_goals=0,
        match_date="2015-04-26", competition="Premier League", stadium="Emirates",
        attendance=60000,
    )
    assert "Still missing" not in response.text(), "the trip's city should count"


def test_add_match_refuses_when_the_trip_does_not_exist(cog, db):
    response = call(
        Trips.add_match, cog, FakeInteraction(), year=1999, home="A", away="B"
    )
    assert "No 1999 trip yet" in response.text()
    assert db.query("SELECT * FROM trip_matches") == []


# -- goals ------------------------------------------------------------------


def _trip_with_match(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany")
    call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=0, away_goals=3,
    )
    return db.query_one("SELECT * FROM trip_matches")["id"]


def test_add_goals_stores_the_scorers(cog, db):
    match_id = _trip_with_match(cog, db)
    response = call(
        Trips.add_goals, cog, FakeInteraction(),
        match=str(match_id), goals="23 Robben A, 45+2 Mueller A, 67 Goetze A",
    )
    assert "Recorded 3 goal(s)" in response.text()
    rows = db.query("SELECT * FROM trip_goals ORDER BY minute")
    assert [(r["minute"], r["scorer"], r["side"]) for r in rows] == [
        (23, "Robben", "A"),
        (45, "Mueller", "A"),
        (67, "Goetze", "A"),
    ]


def test_add_goals_appends_by_default_and_replaces_on_request(cog, db):
    match_id = _trip_with_match(cog, db)
    call(Trips.add_goals, cog, FakeInteraction(), match=str(match_id), goals="23 Robben A")
    call(Trips.add_goals, cog, FakeInteraction(), match=str(match_id), goals="45 Mueller A")
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_goals")["n"] == 2

    call(
        Trips.add_goals, cog, FakeInteraction(),
        match=str(match_id), goals="90 Someone H", replace=True,
    )
    rows = db.query("SELECT * FROM trip_goals")
    assert [r["scorer"] for r in rows] == ["Someone"]


def test_add_goals_saves_the_good_ones_and_names_the_bad(cog, db):
    match_id = _trip_with_match(cog, db)
    response = call(
        Trips.add_goals, cog, FakeInteraction(),
        match=str(match_id), goals="23 Robben A, who knows, 67 Goetze A",
    )
    text = response.text()
    assert "Recorded 2 goal(s)" in text
    assert "Skipped 1" in text and "who knows" in text


def test_add_goals_rejects_input_it_cannot_read_at_all(cog, db):
    match_id = _trip_with_match(cog, db)
    response = call(Trips.add_goals, cog, FakeInteraction(), match=str(match_id), goals="nonsense")
    assert "Couldn't read any goals" in response.text()
    assert db.query("SELECT * FROM trip_goals") == []


# -- reading ----------------------------------------------------------------


def test_empty_archive_commands_all_say_so_rather_than_erroring(cog, db):
    for command in (
        Trips.list_trips, Trips.countries, Trips.teams, Trips.trip_statistics,
        Trips.random_trip, Trips.missing,
    ):
        response = call(command, cog, FakeInteraction())
        assert "yet" in response.text().lower(), command.name


def test_stats_answers_how_many_countries_we_have_seen(cog, db):
    for year, country in ((2010, "England"), (2012, "England"), (2014, "Germany")):
        call(Trips.add, cog, FakeInteraction(), year=year, country=country)
    response = call(Trips.trip_statistics, cog, FakeInteraction())
    text = response.text()
    assert "**2** countries" in text
    assert "**3** trips" in text


def test_stats_reports_goals_results_and_the_streak(cog, db):
    fixtures = [
        (2014, "Germany", "Dortmund", "Bayern", 0, 3),
        (2015, "England", "Arsenal", "Chelsea", 0, 0),
        (2016, "Italy", "Roma", "Lazio", 4, 3),
    ]
    for year, country, home, away, hg, ag in fixtures:
        call(Trips.add, cog, FakeInteraction(), year=year, country=country)
        call(
            Trips.add_match, cog, FakeInteraction(),
            year=year, home=home, away=away, home_goals=hg, away_goals=ag,
        )
    text = call(Trips.trip_statistics, cog, FakeInteraction()).text()
    assert "**10** goals" in text
    assert "**3.33** a game" in text
    assert "0-0s endured: **1**" in text
    assert "**3** straight years (2014–2016)" in text
    assert "Roma" in text  # highest scoring


def test_stats_names_the_years_missing_from_the_archive(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2010, country="England")
    call(Trips.add, cog, FakeInteraction(), year=2013, country="Germany")
    text = call(Trips.trip_statistics, cog, FakeInteraction()).text()
    assert "2011, 2012" in text


def test_countries_lists_each_country_with_its_years(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2010, country="England")
    call(Trips.add, cog, FakeInteraction(), year=2012, country="England")
    text = call(Trips.countries, cog, FakeInteraction()).text()
    assert "**England** — 2×" in text and "2010, 2012" in text


def test_teams_flags_a_club_seen_twice(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="England")
    call(Trips.add, cog, FakeInteraction(), year=2015, country="England")
    call(Trips.add_match, cog, FakeInteraction(), year=2014, home="Arsenal", away="Chelsea")
    call(Trips.add_match, cog, FakeInteraction(), year=2015, home="Chelsea", away="Spurs")
    text = call(Trips.teams, cog, FakeInteraction()).text()
    assert "Seen more than once" in text and "**Chelsea** — 2×" in text


def test_show_renders_the_trip_in_full(cog, db):
    match_id = _trip_with_match(cog, db)
    call(Trips.add_goals, cog, FakeInteraction(), match=str(match_id), goals="23 Robben A")
    text = call(Trips.show, cog, FakeInteraction(), year=2014).text()
    assert "2014" in text and "Germany" in text and "23' Robben (A)" in text


def test_missing_lists_the_gaps_then_goes_quiet_once_filled(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany", city="Dortmund")
    assert "no match recorded" in call(Trips.missing, cog, FakeInteraction()).text()

    call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=0, away_goals=3,
        match_date="2014-04-26", competition="Bundesliga", stadium="Signal Iduna Park",
        attendance=80667,
    )
    assert "scorers" in call(Trips.missing, cog, FakeInteraction()).text()

    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    call(Trips.add_goals, cog, FakeInteraction(), match=str(match_id), goals="23 Robben A")
    assert "Nothing missing" in call(Trips.missing, cog, FakeInteraction()).text()


def test_search_finds_a_trip_by_ground(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany")
    call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", stadium="Signal Iduna Park",
    )
    assert "Dortmund" in call(Trips.search, cog, FakeInteraction(), query="iduna").text()


def test_search_says_so_when_nothing_matches(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany")
    assert "Nothing matches" in call(Trips.search, cog, FakeInteraction(), query="zzz").text()


# -- more than one match on a trip ------------------------------------------


def _two_match_trip(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany", city="Dortmund")
    first = call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=0, away_goals=3,
        match_date="2014-04-26", stadium="Signal Iduna Park",
    ).text()
    second = call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Schalke", away="Koeln", home_goals=1, away_goals=1,
        match_date="2014-04-27", city="Gelsenkirchen", stadium="Veltins-Arena",
    ).text()
    return first, second


def test_a_second_match_is_added_to_the_same_trip_and_numbered(cog, db):
    first, second = _two_match_trip(cog, db)
    assert "of 2" not in first, "the first match needs no numbering"
    assert "match 2 of 2" in second
    assert db.query_one("SELECT COUNT(*) AS n FROM trips")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_matches")["n"] == 2


def test_both_matches_on_a_trip_count_towards_the_stats(cog, db):
    _two_match_trip(cog, db)
    text = call(Trips.trip_statistics, cog, FakeInteraction()).text()
    assert "**1** trips" in text
    assert "**2** matches" in text
    assert "**5** goals" in text          # 0-3 plus 1-1
    assert "**2** cities" in text         # Dortmund and Gelsenkirchen
    assert "**2** different grounds" in text


def test_show_lists_every_match_and_names_the_second_city(cog, db):
    _two_match_trip(cog, db)
    text = call(Trips.show, cog, FakeInteraction(), year=2014).text()
    assert "Dortmund **0–3** Bayern" in text
    assert "Schalke **1–1** Koeln" in text
    assert "Gelsenkirchen" in text
    assert text.count("Signal Iduna Park") == 1


def test_list_flags_a_trip_with_more_than_one_match(cog, db):
    _two_match_trip(cog, db)
    assert "(2 matches)" in call(Trips.list_trips, cog, FakeInteraction()).text()


def test_remove_match_deletes_one_and_leaves_the_trip_and_the_other(cog, db):
    _two_match_trip(cog, db)
    doomed = db.query_one("SELECT id FROM trip_matches WHERE home='Schalke'")["id"]
    call(Trips.add_goals, cog, FakeInteraction(), match=str(doomed), goals="12 Huntelaar H")

    response = call(Trips.remove_match, cog, FakeInteraction(user_id=1), match=str(doomed))
    text = response.text()
    assert "Removed" in text and "1 match(es) still on that trip" in text
    assert [r["home"] for r in db.query("SELECT * FROM trip_matches")] == ["Dortmund"]
    assert db.query("SELECT * FROM trip_goals") == [], "its goals go with it"
    assert db.query_one("SELECT COUNT(*) AS n FROM trips")["n"] == 1


def test_remove_match_refuses_someone_elses_trip(cog, db):
    _two_match_trip(cog, db)
    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    response = call(Trips.remove_match, cog, FakeInteraction(user_id=999), match=str(match_id))
    assert "ask them" in response.text()
    assert db.query_one("SELECT COUNT(*) AS n FROM trip_matches")["n"] == 2


# -- removing ---------------------------------------------------------------


def test_remove_deletes_your_own_trip_and_its_matches(cog, db):
    _trip_with_match(cog, db)
    response = call(Trips.remove, cog, FakeInteraction(user_id=1), year=2014)
    assert "Removed" in response.text() and "1 match(es)" in response.text()
    assert db.query("SELECT * FROM trips") == []
    assert db.query("SELECT * FROM trip_matches") == []


def test_remove_refuses_someone_elses_trip(cog, db):
    call(Trips.add, cog, FakeInteraction(user_id=1), year=2014, country="Germany")
    response = call(Trips.remove, cog, FakeInteraction(user_id=999), year=2014)
    assert "ask them" in response.text()
    assert len(db.query("SELECT * FROM trips")) == 1


# -- correcting a match without destroying it -------------------------------


def _match_with_scorers(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2014, country="Germany", city="Dortmund")
    call(
        Trips.add_match, cog, FakeInteraction(),
        year=2014, home="Dortmund", away="Bayern", home_goals=1, away_goals=0,
        match_date="2014-04-26", competition="Bundesliga", stadium="Signal Iduna Park",
        attendance=80667,
    )
    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    call(
        Trips.add_goals, cog, FakeInteraction(),
        match=str(match_id), goals="23 Robben A, 45 Mueller A",
    )
    return match_id


def test_edit_match_changes_only_what_you_supply(cog, db):
    match_id = _match_with_scorers(cog, db)
    response = call(
        Trips.edit_match, cog, FakeInteraction(), match=str(match_id), home_goals=0, away_goals=3
    )
    assert "Updated away_goals, home_goals" in response.text()
    row = db.query_one("SELECT * FROM trip_matches")
    assert (row["home_goals"], row["away_goals"]) == (0, 3)
    assert row["stadium"] == "Signal Iduna Park", "untouched fields stay put"
    assert row["attendance"] == 80667
    assert row["competition"] == "Bundesliga"
    assert row["match_date"] == "2014-04-26"


def test_edit_match_keeps_the_scorers(cog, db):
    # The reason this command exists: trip_goals cascades off trip_matches, so
    # remove-and-re-add would take the scorers with it.
    match_id = _match_with_scorers(cog, db)
    response = call(
        Trips.edit_match, cog, FakeInteraction(), match=str(match_id), home_goals=0, away_goals=3
    )
    assert [r["scorer"] for r in db.query("SELECT * FROM trip_goals ORDER BY minute")] == [
        "Robben",
        "Mueller",
    ]
    assert "2 scorer(s) kept" in response.text()


def test_editing_a_score_to_nil_nil_is_not_read_as_no_input(cog, db):
    # 0 is falsy; a truthiness check here would silently ignore the edit.
    match_id = _match_with_scorers(cog, db)
    call(Trips.edit_match, cog, FakeInteraction(), match=str(match_id), home_goals=0, away_goals=0)
    row = db.query_one("SELECT * FROM trip_matches")
    assert (row["home_goals"], row["away_goals"]) == (0, 0)


def test_editing_a_crowd_to_zero_is_stored(cog, db):
    match_id = _match_with_scorers(cog, db)
    call(Trips.edit_match, cog, FakeInteraction(), match=str(match_id), attendance=0)
    assert db.query_one("SELECT attendance FROM trip_matches")["attendance"] == 0


def test_edit_match_can_correct_the_teams_and_the_ground(cog, db):
    match_id = _match_with_scorers(cog, db)
    call(
        Trips.edit_match, cog, FakeInteraction(), match=str(match_id),
        home="Borussia Dortmund", away="Bayern Munich", stadium="Westfalenstadion",
        city="Dortmund", competition="DFB-Pokal", notes="cup night",
    )
    row = db.query_one("SELECT * FROM trip_matches")
    assert row["home"] == "Borussia Dortmund"
    assert row["away"] == "Bayern Munich"
    assert row["stadium"] == "Westfalenstadion"
    assert row["competition"] == "DFB-Pokal"
    assert row["notes"] == "cup night"


def test_edit_match_rejects_a_bad_date_without_writing(cog, db):
    match_id = _match_with_scorers(cog, db)
    response = call(
        Trips.edit_match, cog, FakeInteraction(),
        match=str(match_id), match_date="26/04/2014", home_goals=9,
    )
    assert "YYYY-MM-DD" in response.text()
    row = db.query_one("SELECT * FROM trip_matches")
    assert (row["match_date"], row["home_goals"]) == ("2014-04-26", 1), "nothing written"


def test_edit_match_with_no_fields_says_so_and_writes_nothing(cog, db):
    match_id = _match_with_scorers(cog, db)
    response = call(Trips.edit_match, cog, FakeInteraction(), match=str(match_id))
    assert "I need at least one" in response.text()
    assert db.query_one("SELECT home_goals FROM trip_matches")["home_goals"] == 1


def test_edit_match_on_an_unknown_match_says_so(cog, db):
    response = call(Trips.edit_match, cog, FakeInteraction(), match="9999", home_goals=1)
    assert "don't have that match" in response.text()


def test_edit_match_reports_what_is_still_missing(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2018, country="Scotland")
    call(Trips.add_match, cog, FakeInteraction(), year=2018, home="Celtic", away="Rangers")
    match_id = db.query_one("SELECT id FROM trip_matches")["id"]
    response = call(
        Trips.edit_match, cog, FakeInteraction(), match=str(match_id), home_goals=2, away_goals=1
    )
    text = response.text()
    assert "Still missing" in text
    assert "stadium" in text and "score" not in text


# -- /trips matches, the flat table -----------------------------------------


def table_rows(response):
    """The table's lines, code fences stripped."""
    e = response.messages[-1][1]["embed"]
    assert len(e.fields) == 0, "content belongs in the description, not a named field"
    return [line for line in e.description.split("\n") if line != "```"]


def a_match(cog, year, home, away, hg=1, ag=0, stadium="Ground", country="Germany"):
    if cog._trip_for_year(year) is None:
        call(Trips.add, cog, FakeInteraction(), year=year, country=country)
    call(
        Trips.add_match, cog, FakeInteraction(),
        year=year, home=home, away=away, home_goals=hg, away_goals=ag, stadium=stadium,
    )


def test_matches_says_so_when_nothing_is_recorded(cog, db):
    response = call(Trips.matches, cog, FakeInteraction())
    assert "No matches recorded yet" in response.text()


def test_matches_header_names_the_columns_in_order(cog, db):
    a_match(cog, 2014, "Dortmund", "Bayern")
    assert table_rows(call(Trips.matches, cog, FakeInteraction()))[0] == (
        "Year  Home         Away         Res  Ground"
    )


def test_a_match_row_renders_exactly_as_agreed(cog, db):
    a_match(cog, 2014, "Dortmund", "Bayern", 0, 3, "Signal Iduna Park")
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    assert rows[1] == "2014  Dortmund     Bayern       0–3  Signal Iduna Park"


def test_the_ground_column_starts_at_the_same_place_on_every_row(cog, db):
    # The ground is last and unpadded, so rows differ in length by design —
    # what has to hold is that the column starts where the header says.
    a_match(cog, 2014, "Dortmund", "Bayern", 0, 3, "Signal Iduna Park")
    a_match(
        cog, 2015, "Wolverhampton Wanderers", "Real Madrid CF", 10, 3,
        "Santiago Bernabeu Stadium",
    )
    a_match(cog, 2016, "Celtic", "Rangers", stadium=None)
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    column = rows[0].index("Ground")
    for row in rows[1:]:
        assert row[column - 1] == " ", f"column drifted: {row!r}"
        assert row[column] != " ", f"ground missing at the column: {row!r}"


def test_a_double_digit_scoreline_does_not_shift_the_ground(cog, db):
    # The reason the result column is 4 wide rather than 3.
    a_match(cog, 2014, "Dortmund", "Bayern", 0, 3, "Signal Iduna Park")
    a_match(cog, 2015, "Arsenal", "Chelsea", 10, 3, "Emirates", country="England")
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    column = rows[0].index("Ground")
    assert rows[1][column:].startswith("Signal")
    assert rows[2][column:].startswith("Emirates")


def test_an_unrecorded_score_shows_a_dash_and_stays_aligned(cog, db):
    call(Trips.add, cog, FakeInteraction(), year=2018, country="Scotland")
    call(Trips.add_match, cog, FakeInteraction(), year=2018, home="Celtic", away="Rangers")
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    column = rows[0].index("Ground")
    assert "–" in rows[1][:column]
    assert rows[1][column] == "—", "no ground recorded either"


def test_long_names_are_truncated_rather_than_widening_the_row(cog, db):
    a_match(
        cog, 2019, "Wolverhampton Wanderers", "Real Madrid CF", 1, 0,
        "Santiago Bernabeu Stadium",
    )
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    column = rows[0].index("Ground")
    assert "…" in rows[1]
    assert rows[1][column - 1] == " "


def test_matches_are_listed_chronologically_including_two_on_one_trip(cog, db):
    a_match(cog, 2016, "Roma", "Lazio", 4, 3, "Olimpico", country="Italy")
    a_match(cog, 2014, "Dortmund", "Bayern", 0, 3, "Signal Iduna Park")
    a_match(cog, 2014, "Schalke", "Koeln", 1, 1, "Veltins-Arena")
    rows = table_rows(call(Trips.matches, cog, FakeInteraction()))
    years = [row[:4] for row in rows[1:]]
    assert years == ["2014", "2014", "2016"]


def test_the_footer_counts_matches_and_trips(cog, db):
    a_match(cog, 2014, "Dortmund", "Bayern")
    a_match(cog, 2014, "Schalke", "Koeln")
    a_match(cog, 2016, "Roma", "Lazio", country="Italy")
    e = call(Trips.matches, cog, FakeInteraction()).messages[-1][1]["embed"]
    assert e.footer.text == "3 matches · 2 trips"
