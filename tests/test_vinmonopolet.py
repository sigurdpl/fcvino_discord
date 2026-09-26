"""Matching the club's bottles to Vinmonopolet's catalogue.

Nothing here touches the network: the fetcher is injected, and the fixtures are
real answers the catalogue gave for real wines in this cellar.

What these are mostly about is the failure that matters. A bottle with no
picture costs nothing; a bottle wearing another grower's label is a lie in an
archive meant to outlive the laptop, and both of the wrong matches pinned below
were ones the first version of this happily made.
"""

from __future__ import annotations

import pytest

from bot.vinmonopolet import (
    Match,
    best_match,
    confidence,
    grapes_clash,
    same_producer,
    searchable,
    vintage_of,
)


def catalogue(*names):
    """A stubbed Vinmonopolet that answers with these products, in order."""
    def fetch(url):
        return {"products": [
            {"code": f"{1000 + i}01", "name": name,
             "main_country": {"name": "Italia"}, "district": {"name": "Piemonte"}}
            for i, name in enumerate(names)
        ]}
    return fetch


# -- asking a question the catalogue can answer -----------------------------


@pytest.mark.parametrize("stored, asked", [
    ("Terrazas Afincado Malbec 2008 (330 kr)", "Terrazas Afincado Malbec"),
    ("Don Melchor 2007 98% Cab, 2% Cab Franc", "Don Melchor"),
    ("Colome Lote Especial 2013 (Morten)", "Colome Lote Especial"),
    ("Ch. Musar 2005", "Ch. Musar"),
    ("Massolino Barolo", "Massolino Barolo"),
])
def test_the_spreadsheets_clutter_comes_out_of_the_question(stored, asked):
    """The club's older names carry the price and the blend inside them, and
    those turn a findable wine into nothing at all. Stripping them took a
    sample of the cellar from 6/15 matches to 11/15."""
    assert searchable(stored) == asked


def test_the_vintage_goes_too():
    """They stock the year they have, not the year the club drank."""
    assert "2015" not in searchable("Massolino Barolo 2015")
    assert vintage_of("Massolino Barolo 2015") == 2015
    assert vintage_of("Dom. de Saint Amand") is None


# -- how sure is sure enough ------------------------------------------------


def test_confidence_is_how_much_of_our_name_theirs_accounts_for():
    assert confidence("Massolino Barolo 2015", "Massolino Barolo 2019") == 1.0
    assert confidence("Massolino Barolo 2015", "Vietti Barolo Castiglione") == 0.5
    assert confidence("Massolino Barolo", "Ch. Musar") == 0.0


def test_a_wine_that_shares_only_its_grape_is_not_a_match():
    found = best_match("Weinert Malbec 2004", fetch=catalogue("Trapiche Malbec 2020"))
    assert found is None


# -- the two wrong matches this used to make --------------------------------


def test_the_same_vineyard_is_not_the_same_grower():
    """Anselma and Massolino both make a Barolo from Vigna Rionda. Three words
    of four in common, and the wrong bottle — the name in front decides it."""
    theirs = "Massolino Barolo Vigna Rionda Riserva"
    assert confidence("Anselma Barolo Vigna Rionda 2007", theirs) == 0.75
    assert not same_producer("Anselma Barolo Vigna Rionda 2007", theirs)
    assert best_match("Anselma Barolo Vigna Rionda 2007", fetch=catalogue(theirs)) is None


def test_the_same_grower_is_not_the_same_grape():
    """Chiara Boschis makes both; the village is the same and the bottle is not."""
    ours, theirs = "Chiara Boschis Dolcetto d'Alba 2011", "E. Pira di Chiara Boschis Barbera d'Alba"
    assert same_producer(ours, theirs), "same grower, so overlap alone would pass it"
    assert grapes_clash(ours, theirs)
    assert best_match(ours, fetch=catalogue(theirs)) is None


def test_the_grower_and_the_grape_agreeing_is_a_match():
    ours = "Chiara Boschis Barbera d'Alba 2011"
    found = best_match(ours, fetch=catalogue("E. Pira di Chiara Boschis Barbera d'Alba 2023"))
    assert found is not None and found.code == "100001"


# -- what a match is --------------------------------------------------------


def test_a_match_carries_the_code_and_builds_its_own_picture():
    found = best_match("Boroli Barolo 2005", fetch=catalogue("Boroli Barolo 2013"))
    assert found.code == "100001"
    assert found.vintage == 2013, "theirs, not ours"
    assert found.image(300) == "https://bilder.vinmonopolet.no/cache/300x300-0/100001-1.jpg"
    assert found.image(96).startswith("https://bilder.vinmonopolet.no/cache/96x96-0/")


def test_our_typo_does_not_lose_a_real_match():
    """`Lavignone Barebera d'Asti` is ours, misspelled, and still theirs."""
    found = best_match("Lavignone Barebera d'Asti 2011",
                       fetch=catalogue("Lavignone Barbera d'Asti 2024"))
    assert found is not None


def test_the_best_of_several_answers_wins():
    found = best_match(
        "Jaboulet Crozes Hermitage Les Jalets 2010",
        fetch=catalogue("Jaboulet Parallele 45", "Jaboulet Crozes Hermitage Les Jalets 2023"),
    )
    assert "Les Jalets" in found.name


def test_a_catalogue_that_is_down_is_not_a_crash():
    """One shop's website being cross is no reason to stop a run of a thousand."""
    def broken(url):
        raise TimeoutError("vinmonopolet.no took too long")

    assert best_match("Ch. Musar 2005", fetch=broken) is None


def test_nothing_found_is_nothing_written():
    assert best_match("A wine nobody sells", fetch=catalogue()) is None
    assert best_match("", fetch=catalogue("Anything")) is None


def test_a_match_is_immutable():
    """It is passed around and rendered; nothing downstream may edit it."""
    found = Match(code="1", name="x", country=None, district=None, vintage=None, score=1.0)
    with pytest.raises(AttributeError):
        found.code = "2"
