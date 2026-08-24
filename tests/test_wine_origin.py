"""Placing a wine from its name and its tasting.

The cases are real names out of the club's spreadsheet, not invented ones, so a
regression here is a regression against the actual archive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import wine_origin
from bot.wine_origin import clean_name, derive, fold, is_non_wine, parse_vintage

# -- folding ----------------------------------------------------------------


def test_accents_are_folded_away():
    assert fold("Côte-Rôtie").strip() == "cote rotie"
    assert fold("Château").strip() == "chateau"


@pytest.mark.parametrize(
    "raw,expected",
    [("Sør Afrika", "sor afrika"), ("Øst-Europa", "ost europa"), ("Ærø", "aero")],
)
def test_norwegian_letters_are_spelled_out(raw, expected):
    # NFKD leaves ø, æ and å alone, so without transliteration "Sør Afrika"
    # folds to "s r afrika" and matches nothing — losing ten South African wines.
    assert fold(raw).strip() == expected


def test_folding_is_padded_so_matches_are_whole_words():
    # "toro" must not match inside another word.
    assert fold("Toro") == " toro "


# -- the appellation is the grape -------------------------------------------


@pytest.mark.parametrize(
    "name,country,region,grape",
    [
        ("Boroli Barolo 2005", "Italy", "Barolo", "Nebbiolo"),
        ("Anselma Barolo Vigna Rionda 2007", "Italy", "Barolo", "Nebbiolo"),
        ("Clusel Roch Côte-Rôtie 2007", "France", "Côte-Rôtie", "Syrah"),
        ("Louis Jadot Chablis Premier Cru 2015", "France", "Chablis", "Chardonnay"),
        ("Clément Gevrey-Chambertin Evocelles 15", "France", "Gevrey-Chambertin", "Pinot Noir"),
        ("Il Poggione Brunello di Montalcino 2016", "Italy", "Brunello di Montalcino",
         "Sangiovese"),
        ("Casanova di Neri Brunello 2007", "Italy", "Brunello di Montalcino", "Sangiovese"),
        ("Jean Louis Chave Hermitage", "France", "Hermitage", "Syrah"),
        ("Clos des Brusquières Chateauneuf-du-Pape 2019", "France", "Châteauneuf-du-Pape",
         "Grenache blend"),
        ("Szepsy Tokaji Furmint 2015", "Hungary", "Tokaj", "Furmint"),
    ],
)
def test_classic_appellations_give_country_and_grape(name, country, region, grape):
    assert derive(name) == (country, region, grape)


def test_the_longest_appellation_wins():
    # Crozes-Hermitage is its own place, not Hermitage.
    assert derive("Graillot Crozes-Hermitage 2018").region == "Crozes-Hermitage"
    assert derive("Chave Hermitage 2015").region == "Hermitage"


def test_a_grape_printed_on_the_label_is_read():
    assert derive("Casillero del Diablo Cabernet Sauvignon 2011").grape == "Cabernet Sauvignon"
    assert derive("Ridge Lytton Springs Zinfandel 2011").grape == "Zinfandel"


# -- synonyms ---------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("Penfolds Shiraz 2015", "Chapoutier Syrah 2015"),
        ("Huber Spätburgunder 2019", "Drouhin Pinot Noir 2019"),
        ("Alvaro Palacios Garnacha", "Ogier Grenache"),
        ("Casa Castillo Monastrell", "Tempier Mourvèdre"),
    ],
)
def test_synonyms_fold_onto_one_grape(left, right):
    # Otherwise a grape board lists the same grape twice and halves its count.
    assert derive(left).grape == derive(right).grape


def test_tinta_de_toro_is_tempranillo():
    assert derive("Numanthia Tinta de Toro 2018").grape == "Tempranillo"


# -- refusing to guess ------------------------------------------------------


def test_a_theme_naming_two_countries_settles_nothing():
    # "Argentina vs Chile" — picking one would file a Chilean Cabernet under
    # Argentina, which is exactly what happened before this rule.
    origin = derive("Casillero del Diablo Cabernet Sauvignon 2011", "Argentina vs Chile")
    assert origin.country is None
    assert origin.grape == "Cabernet Sauvignon", "the grape is still on the label"


def test_a_theme_spanning_two_countries_settles_no_region():
    origin = derive("Szepsy Tokaji Furmint 2015", "Tokaji & Toscana")
    assert (origin.country, origin.region) == ("Hungary", "Tokaj"), "the name wins"
    assert derive("Some Unknown Bottle 2015", "Tokaji & Toscana").region is None


def test_the_name_outranks_the_theme():
    # A Bordeaux evening can still hold a ringer, and several of these did.
    assert derive("Boroli Barolo 2005", "Bordeaux").country == "Italy"


def test_an_unplaceable_wine_stays_empty():
    origin = derive("Mother Rock Brutal!", "NYDELI")
    assert origin.empty


# -- countries in Norwegian -------------------------------------------------


@pytest.mark.parametrize(
    "name,country",
    [
        ("Protos Reserva 2015 (Spania)", "Spain"),
        ("Garzón Single Vineyard Tannat 2018 (Uruguay)", "Uruguay"),
        ("Casa Valduga Villa Lobos Cabernet Sauvignon 2015 (Brasil)", "Brazil"),
        ("Blackbook Greyfriars Chardonnay 2018 (England)", "England"),
    ],
)
def test_a_bracketed_country_is_read(name, country):
    assert derive(name).country == country


def test_a_norwegian_theme_gives_the_country():
    assert derive("Kanonkop Paul Sauer 2017", "Sør Afrika").country == "South Africa"


# -- grapes that imply a country --------------------------------------------


def test_a_grape_grown_in_one_place_implies_the_country():
    assert derive("Palladino Barbera Superiore 2012").country == "Italy"
    assert derive("Filipa Pato Baga 2018").country == "Portugal"


def test_a_widely_planted_grape_implies_nothing():
    # Syrah is everywhere; claiming France would be a coin toss.
    assert derive("Some Producer Syrah 2018").country is None


# -- not wine ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["1. Chimay Trappist Red (Belgia)",
                                  "2. Mikkeller Beer Geek Breakfast (Danmark)"])
def test_a_beer_gets_a_country_but_never_a_grape(name):
    origin = derive(name, "Thomas sine fristelser")
    assert origin.country is not None
    assert origin.grape is None
    assert is_non_wine(name)


def test_wine_is_not_mistaken_for_beer():
    assert not is_non_wine("Filipa Pato Post Quer..s Baga")
    assert not is_non_wine("Boroli Barolo 2005")


# -- names and vintages -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1. Vin 1: Ch. Panet 2015", "Ch. Panet 2015"),
        ("10. Ridge Geyserville 2020", "Ridge Geyserville 2020"),
        ("7. Vin7 Viuva Gomes Colares 2012", "Viuva Gomes Colares 2012"),
        ("2) Some Wine", "Some Wine"),
        ("Boroli Barolo 2005", "Boroli Barolo 2005"),
    ],
)
def test_pouring_order_labels_are_stripped(raw, expected):
    assert clean_name(raw) == expected


def test_content_after_the_number_is_kept():
    # "Lennart 1 Toscana" says who brought it and where from — not noise.
    assert clean_name("3. Lennart 1 Toscana Il Poggione Brunello 2016").startswith("Lennart")


@pytest.mark.parametrize(
    "name,vintage",
    [
        ("Boroli Barolo 2005", 2005),
        ("Ch. Musar 1998", 1998),
        ("Viúva Gomes Colares 1969", 1969),
        ("Mother Rock Brutal!", None),
        ("Protos Reserva 09", None),          # two digits are too ambiguous to use
        ("Don Melchor 2007 98% Cab (549kr)", 2007),
    ],
)
def test_vintage_is_read_only_when_unambiguous(name, vintage):
    assert parse_vintage(name) == vintage


def test_a_future_vintage_is_ignored():
    assert parse_vintage("Some Wine 2099", latest=2026) is None


# -- the shipped reference data ---------------------------------------------


def test_every_appellation_grape_is_a_known_grape():
    """An appellation's grape must be spelled the same as the grape table's.

    Otherwise the same grape appears twice on a board — once from the label and
    once from the appellation — with different spellings.
    """
    canonical = set(wine_origin.GRAPES.values()) | {
        wine_origin.BORDEAUX_BLEND,
        wine_origin.RHONE_BLEND,
        wine_origin.PORT_BLEND,
        wine_origin.CHAMPAGNE_BLEND,
    }
    for key, (_, _, grape) in wine_origin.APPELLATIONS.items():
        assert grape is None or grape in canonical, f"{key} -> {grape}"


def test_every_homeland_grape_is_a_known_grape():
    canonical = set(wine_origin.GRAPES.values())
    for grape in wine_origin.GRAPE_HOMELAND:
        assert grape in canonical, grape


def test_reference_keys_are_folded_already():
    """Keys are matched against folded text, so an unfolded key never matches."""
    for table in (wine_origin.APPELLATIONS, wine_origin.GRAPES, wine_origin.COUNTRIES):
        for key in table:
            assert fold(key).strip() == key, key


def test_the_researched_seed_file_is_valid():
    payload = json.loads((Path("seeds/wine_origins.json")).read_text())
    for entry in payload["wines"]:
        assert entry.get("name"), entry
        assert entry.get("source"), f"every claim needs a source: {entry['name']}"
        assert entry.get("country") or entry.get("grape") or entry.get("note"), entry
