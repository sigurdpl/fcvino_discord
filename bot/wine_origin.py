"""Working out where a wine is from, and what it's made of, from its name.

Pure reference data plus matching — no database, no network. `scripts/enrich_wines.py`
applies it; anything this can't place is looked up by hand and recorded in
`seeds/wine_origins.json`.

The leverage here is that across classical Europe **the appellation is the grape**:
a Barolo is Nebbiolo, a Chablis is Chardonnay, a Gevrey-Chambertin is Pinot Noir.
That is law, not inference, so it can be tabulated rather than guessed at. Outside
Europe the grape is usually printed on the label and so sits in the wine's name.

Matching is on folded text — lowercase, accents stripped, punctuation flattened —
because the spreadsheet spells things every possible way: `Côte-Rôtie` and
`Cote Rotie`, `Spätburgunder` and `Spatburgunder`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

FRANCE, ITALY, SPAIN, PORTUGAL = "France", "Italy", "Spain", "Portugal"
GERMANY, AUSTRIA, HUNGARY, GREECE = "Germany", "Austria", "Hungary", "Greece"
USA, CHILE, ARGENTINA, AUSTRALIA = "USA", "Chile", "Argentina", "Australia"
NZ, SOUTH_AFRICA, LEBANON = "New Zealand", "South Africa", "Lebanon"

BORDEAUX_BLEND = "Bordeaux blend"
RHONE_BLEND = "Grenache blend"
PORT_BLEND = "Touriga Nacional blend"
CHAMPAGNE_BLEND = "Champagne blend"


class Origin(NamedTuple):
    country: str | None
    region: str | None
    grape: str | None

    @property
    def empty(self) -> bool:
        return not (self.country or self.region or self.grape)


# Letters NFKD will not take apart, so they have to be spelled out. Without
# this, fold("Sør Afrika") comes out as "s r afrika" and never matches anything
# — which silently loses every Norwegian word carrying ø, å or æ.
TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
    "ß": "ss", "đ": "d", "ð": "d", "þ": "th", "ł": "l",
})


def fold(text: str) -> str:
    """Lowercase, strip accents, flatten punctuation to single spaces."""
    spelled = text.lower().translate(TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", spelled)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " " + re.sub(r"[^a-z0-9]+", " ", stripped).strip() + " "


# Grape synonyms folded onto one spelling. Without this a grape board splits the
# same grape across two rows: Shiraz under S and Syrah somewhere else.
GRAPES: dict[str, str] = {
    "syrah": "Syrah", "shiraz": "Syrah",
    "pinot noir": "Pinot Noir", "spatburgunder": "Pinot Noir",
    "pinot nero": "Pinot Noir", "blauburgunder": "Pinot Noir",
    "pinot blanc": "Pinot Blanc", "weissburgunder": "Pinot Blanc",
    "pinot bianco": "Pinot Blanc",
    "pinot gris": "Pinot Gris", "pinot grigio": "Pinot Gris",
    "grauburgunder": "Pinot Gris", "rulander": "Pinot Gris",
    "chardonnay": "Chardonnay",
    "riesling": "Riesling",
    "sauvignon blanc": "Sauvignon Blanc", "sauvignon gris": "Sauvignon Blanc",
    "cabernet sauvignon": "Cabernet Sauvignon",
    # "Cabernet" alone almost always means Sauvignon; longest-match still sends
    # "Cabernet Franc" to the right place. "Cab S" is how the sheet abbreviates it.
    "cabernet": "Cabernet Sauvignon", "cab s": "Cabernet Sauvignon",
    "cab sauv": "Cabernet Sauvignon",
    "cabernet franc": "Cabernet Franc",
    "merlot": "Merlot",
    "malbec": "Malbec", "cot": "Malbec",
    "petit verdot": "Petit Verdot",
    "carmenere": "Carmenère",
    "tannat": "Tannat",
    "nebbiolo": "Nebbiolo",
    "sangiovese": "Sangiovese",
    "barbera": "Barbera",
    "dolcetto": "Dolcetto",
    "corvina": "Corvina",
    "aglianico": "Aglianico",
    "nero d avola": "Nero d'Avola",
    "nerello mascalese": "Nerello Mascalese", "nerello": "Nerello Mascalese",
    "frappato": "Frappato",
    "primitivo": "Zinfandel", "zinfandel": "Zinfandel",  # the same grape
    "negroamaro": "Negroamaro",
    "montepulciano d abruzzo": "Montepulciano",
    "garganega": "Garganega",
    "vermentino": "Vermentino",
    "cortese": "Cortese",
    "glera": "Glera",
    "tempranillo": "Tempranillo", "tinta de toro": "Tempranillo",
    "tinto fino": "Tempranillo", "tinta del pais": "Tempranillo",
    "aragonez": "Tempranillo",
    "garnacha": "Grenache", "grenache": "Grenache", "cannonau": "Grenache",
    "carinena": "Carignan", "carignan": "Carignan", "mazuelo": "Carignan",
    "monastrell": "Mourvèdre", "mourvedre": "Mourvèdre",
    "graciano": "Graciano",
    "mencia": "Mencía",
    "albarino": "Albariño", "alvarinho": "Albariño",
    "verdejo": "Verdejo",
    "godello": "Godello",
    "palomino": "Palomino",
    "baga": "Baga",
    "ramisco": "Ramisco",
    "touriga nacional": "Touriga Nacional", "touriga franca": "Touriga Franca",
    "tinta roriz": "Tempranillo",
    "gewurztraminer": "Gewürztraminer", "traminer": "Gewürztraminer",
    "silvaner": "Silvaner", "sylvaner": "Silvaner",
    "muller thurgau": "Müller-Thurgau",
    "gruner veltliner": "Grüner Veltliner", "gruner": "Grüner Veltliner",
    "blaufrankisch": "Blaufränkisch", "lemberger": "Blaufränkisch",
    "zweigelt": "Zweigelt",
    "sankt laurent": "St. Laurent", "st laurent": "St. Laurent",
    "furmint": "Furmint",
    "assyrtiko": "Assyrtiko",
    "xinomavro": "Xinomavro",
    "agiorgitiko": "Agiorgitiko", "agiorgitikoo": "Agiorgitiko",
    "saperavi": "Saperavi", "superavi": "Saperavi",  # spelled thus in the sheet
    "chenin blanc": "Chenin Blanc", "chenin": "Chenin Blanc",
    "pinotage": "Pinotage",
    "cinsault": "Cinsault", "cincault": "Cinsault",
    "semillon": "Sémillon",
    "viognier": "Viognier",
    "marsanne": "Marsanne", "roussanne": "Roussanne",
    "gamay": "Gamay",
    "melon de bourgogne": "Melon de Bourgogne",
    "savagnin": "Savagnin", "poulsard": "Poulsard", "trousseau": "Trousseau",
    "mondeuse": "Mondeuse",
    "malvasia": "Malvasia",
    "moscato": "Muscat", "muscat": "Muscat", "muskat": "Muscat",
    "pedro ximenez": "Pedro Ximénez",
    "arinto": "Arinto",
    "fernao pires": "Fernão Pires", "ferano pires": "Fernão Pires",
    "grillo": "Grillo",
    "cesanese": "Cesanese",
    "schioppettino": "Schioppettino",
    "refosco": "Refosco",
    "lagrein": "Lagrein",
    "teroldego": "Teroldego",
    "ribolla": "Ribolla Gialla", "rebula": "Ribolla Gialla",
    "friulano": "Friulano",
    "turbiana": "Turbiana",
    "verdicchio": "Verdicchio",
    "xarel lo": "Xarel-lo",
    "blauer wildbacher": "Blauer Wildbacher",
    "chiavennasca": "Nebbiolo",  # what Valtellina calls Nebbiolo
}

# Appellation or region -> (country, region shown, the grape it is by law or
# overwhelming convention). None where the region genuinely grows many things.
APPELLATIONS: dict[str, tuple[str, str, str | None]] = {
    # -- France: Bordeaux -------------------------------------------------
    "margaux": (FRANCE, "Margaux", BORDEAUX_BLEND),
    "pauillac": (FRANCE, "Pauillac", BORDEAUX_BLEND),
    "saint julien": (FRANCE, "Saint-Julien", BORDEAUX_BLEND),
    "saint estephe": (FRANCE, "Saint-Estèphe", BORDEAUX_BLEND),
    "lalande de pomerol": (FRANCE, "Lalande-de-Pomerol", "Merlot"),
    "pomerol": (FRANCE, "Pomerol", "Merlot"),
    "saint emilion": (FRANCE, "Saint-Émilion", "Merlot"),
    "listrac": (FRANCE, "Listrac-Médoc", BORDEAUX_BLEND),
    "moulis": (FRANCE, "Moulis-en-Médoc", BORDEAUX_BLEND),
    "haut medoc": (FRANCE, "Haut-Médoc", BORDEAUX_BLEND),
    "medoc": (FRANCE, "Médoc", BORDEAUX_BLEND),
    "pessac leognan": (FRANCE, "Pessac-Léognan", BORDEAUX_BLEND),
    "graves": (FRANCE, "Graves", BORDEAUX_BLEND),
    "sauternes": (FRANCE, "Sauternes", "Sémillon"),
    "barsac": (FRANCE, "Barsac", "Sémillon"),
    "fronsac": (FRANCE, "Fronsac", "Merlot"),
    "castillon": (FRANCE, "Castillon", "Merlot"),
    "bordeaux": (FRANCE, "Bordeaux", BORDEAUX_BLEND),
    "bordaux": (FRANCE, "Bordeaux", BORDEAUX_BLEND),  # spelled this way in a theme
    # -- France: Burgundy -------------------------------------------------
    "chablis": (FRANCE, "Chablis", "Chardonnay"),
    "montee de tonnerre": (FRANCE, "Chablis", "Chardonnay"),
    "montmains": (FRANCE, "Chablis", "Chardonnay"),
    "gevrey chambertin": (FRANCE, "Gevrey-Chambertin", "Pinot Noir"),
    "chambolle musigny": (FRANCE, "Chambolle-Musigny", "Pinot Noir"),
    "morey saint denis": (FRANCE, "Morey-Saint-Denis", "Pinot Noir"),
    "vosne romanee": (FRANCE, "Vosne-Romanée", "Pinot Noir"),
    "nuits saint georges": (FRANCE, "Nuits-Saint-Georges", "Pinot Noir"),
    "savigny": (FRANCE, "Savigny-lès-Beaune", "Pinot Noir"),
    "aloxe corton": (FRANCE, "Aloxe-Corton", "Pinot Noir"),
    "pernand": (FRANCE, "Pernand-Vergelesses", "Pinot Noir"),
    "pommard": (FRANCE, "Pommard", "Pinot Noir"),
    "volnay": (FRANCE, "Volnay", "Pinot Noir"),
    "monthelie": (FRANCE, "Monthélie", "Pinot Noir"),
    "auxey duresses": (FRANCE, "Auxey-Duresses", "Pinot Noir"),
    "auxey durresses": (FRANCE, "Auxey-Duresses", "Pinot Noir"),
    "marsannay": (FRANCE, "Marsannay", "Pinot Noir"),
    "fixin": (FRANCE, "Fixin", "Pinot Noir"),
    "ladoix": (FRANCE, "Ladoix", "Pinot Noir"),
    "echezeaux": (FRANCE, "Échezeaux", "Pinot Noir"),
    "clos de vougeot": (FRANCE, "Clos de Vougeot", "Pinot Noir"),
    "bonnes mares": (FRANCE, "Bonnes-Mares", "Pinot Noir"),
    "richebourg": (FRANCE, "Richebourg", "Pinot Noir"),
    "musigny": (FRANCE, "Musigny", "Pinot Noir"),
    "meursault": (FRANCE, "Meursault", "Chardonnay"),
    "puligny montrachet": (FRANCE, "Puligny-Montrachet", "Chardonnay"),
    "chassagne montrachet": (FRANCE, "Chassagne-Montrachet", "Chardonnay"),
    "montrachet": (FRANCE, "Montrachet", "Chardonnay"),
    "corton charlemagne": (FRANCE, "Corton-Charlemagne", "Chardonnay"),
    "santenay": (FRANCE, "Santenay", "Pinot Noir"),
    "mercurey": (FRANCE, "Mercurey", "Pinot Noir"),
    "mercury": (FRANCE, "Mercurey", "Pinot Noir"),  # spelled this way in the sheet
    "rully": (FRANCE, "Rully", "Chardonnay"),
    "givry": (FRANCE, "Givry", "Pinot Noir"),
    "montagny": (FRANCE, "Montagny", "Chardonnay"),
    "pouilly fuisse": (FRANCE, "Pouilly-Fuissé", "Chardonnay"),
    "saint veran": (FRANCE, "Saint-Véran", "Chardonnay"),
    "macon": (FRANCE, "Mâcon", "Chardonnay"),
    "beaune": (FRANCE, "Beaune", "Pinot Noir"),
    "coteaux bourguignons": (FRANCE, "Bourgogne", None),
    "saint romain": (FRANCE, "Saint-Romain", None),
    "bourgogne": (FRANCE, "Bourgogne", None),
    "burgund": (FRANCE, "Bourgogne", None),
    # -- France: Rhône ----------------------------------------------------
    "cote rotie": (FRANCE, "Côte-Rôtie", "Syrah"),
    "crozes hermitage": (FRANCE, "Crozes-Hermitage", "Syrah"),
    "hermitage": (FRANCE, "Hermitage", "Syrah"),
    "cornas": (FRANCE, "Cornas", "Syrah"),
    "saint joseph": (FRANCE, "Saint-Joseph", "Syrah"),
    "st joseph": (FRANCE, "Saint-Joseph", "Syrah"),
    "condrieu": (FRANCE, "Condrieu", "Viognier"),
    "chateauneuf": (FRANCE, "Châteauneuf-du-Pape", RHONE_BLEND),
    "gigondas": (FRANCE, "Gigondas", RHONE_BLEND),
    "vacqueyras": (FRANCE, "Vacqueyras", RHONE_BLEND),
    "rasteau": (FRANCE, "Rasteau", RHONE_BLEND),
    "cairanne": (FRANCE, "Cairanne", RHONE_BLEND),
    "lirac": (FRANCE, "Lirac", RHONE_BLEND),
    "ventoux": (FRANCE, "Ventoux", RHONE_BLEND),
    "luberon": (FRANCE, "Luberon", RHONE_BLEND),
    "cotes du rhone": (FRANCE, "Côtes du Rhône", RHONE_BLEND),
    "cote du rhone": (FRANCE, "Côtes du Rhône", RHONE_BLEND),
    "rhone": (FRANCE, "Rhône", None),
    # -- France: Loire, Alsace, Champagne, Jura, Beaujolais ---------------
    "sancerre": (FRANCE, "Sancerre", "Sauvignon Blanc"),
    "pouilly fume": (FRANCE, "Pouilly-Fumé", "Sauvignon Blanc"),
    "chinon": (FRANCE, "Chinon", "Cabernet Franc"),
    "bourgueil": (FRANCE, "Bourgueil", "Cabernet Franc"),
    "saumur": (FRANCE, "Saumur", "Cabernet Franc"),
    "vouvray": (FRANCE, "Vouvray", "Chenin Blanc"),
    "savennieres": (FRANCE, "Savennières", "Chenin Blanc"),
    "muscadet": (FRANCE, "Muscadet", "Melon de Bourgogne"),
    "coteaux du layon": (FRANCE, "Coteaux du Layon", "Chenin Blanc"),
    "alsace": (FRANCE, "Alsace", None),
    "champagne": (FRANCE, "Champagne", CHAMPAGNE_BLEND),
    "ambonnay": (FRANCE, "Champagne", CHAMPAGNE_BLEND),  # a Champagne grand cru village
    "arbois": (FRANCE, "Arbois", None),
    "cotes du jura": (FRANCE, "Côtes du Jura", None),
    "jura": (FRANCE, "Jura", None),
    "morgon": (FRANCE, "Morgon", "Gamay"),
    "fleurie": (FRANCE, "Fleurie", "Gamay"),
    "moulin a vent": (FRANCE, "Moulin-à-Vent", "Gamay"),
    "brouilly": (FRANCE, "Brouilly", "Gamay"),
    "julienas": (FRANCE, "Juliénas", "Gamay"),
    "chenas": (FRANCE, "Chénas", "Gamay"),
    "beaujolais": (FRANCE, "Beaujolais", "Gamay"),
    "bandol": (FRANCE, "Bandol", "Mourvèdre"),
    "corbieres": (FRANCE, "Corbières", RHONE_BLEND),
    "minervois": (FRANCE, "Minervois", RHONE_BLEND),
    "faugeres": (FRANCE, "Faugères", RHONE_BLEND),
    "pic saint loup": (FRANCE, "Pic Saint-Loup", RHONE_BLEND),
    "terrasses du larzac": (FRANCE, "Terrasses du Larzac", RHONE_BLEND),
    "saint chinian": (FRANCE, "Saint-Chinian", RHONE_BLEND),
    "cotes catalanes": (FRANCE, "Côtes Catalanes", None),
    "roussillon": (FRANCE, "Roussillon", None),
    "vin de france": (FRANCE, None, None),
    "languedoc": (FRANCE, "Languedoc", None),
    "provence": (FRANCE, "Provence", None),
    "cotes de provence": (FRANCE, "Côtes de Provence", None),
    "cahors": (FRANCE, "Cahors", "Malbec"),
    "madiran": (FRANCE, "Madiran", "Tannat"),
    "jurancon": (FRANCE, "Jurançon", None),
    # -- Italy ------------------------------------------------------------
    "barolo": (ITALY, "Barolo", "Nebbiolo"),
    "barbaresco": (ITALY, "Barbaresco", "Nebbiolo"),
    "gattinara": (ITALY, "Gattinara", "Nebbiolo"),
    "ghemme": (ITALY, "Ghemme", "Nebbiolo"),
    "roero": (ITALY, "Roero", "Nebbiolo"),
    "langhe": (ITALY, "Langhe", None),
    "brunello di montalcino": (ITALY, "Brunello di Montalcino", "Sangiovese"),
    "brunello": (ITALY, "Brunello di Montalcino", "Sangiovese"),
    "rosso di montalcino": (ITALY, "Rosso di Montalcino", "Sangiovese"),
    "montalcino": (ITALY, "Montalcino", "Sangiovese"),
    "vino nobile": (ITALY, "Vino Nobile di Montepulciano", "Sangiovese"),
    "chianti": (ITALY, "Chianti", "Sangiovese"),
    "carmignano": (ITALY, "Carmignano", "Sangiovese"),
    "morellino": (ITALY, "Morellino di Scansano", "Sangiovese"),
    "bolgheri": (ITALY, "Bolgheri", BORDEAUX_BLEND),
    "maremma": (ITALY, "Maremma", None),
    "toscana": (ITALY, "Toscana", None),
    "amarone": (ITALY, "Amarone della Valpolicella", "Corvina"),
    "valpolicella": (ITALY, "Valpolicella", "Corvina"),
    "ripasso": (ITALY, "Valpolicella Ripasso", "Corvina"),
    "bardolino": (ITALY, "Bardolino", "Corvina"),
    "soave": (ITALY, "Soave", "Garganega"),
    "lugana": (ITALY, "Lugana", "Turbiana"),
    "valtellina": (ITALY, "Valtellina", "Nebbiolo"),
    "sant antimo": (ITALY, "Sant'Antimo", None),
    "castelli di jesi": (ITALY, "Castelli di Jesi", "Verdicchio"),
    "marche": (ITALY, "Marche", None),
    "etna": (ITALY, "Etna", "Nerello Mascalese"),
    "cerasuolo di vittoria": (ITALY, "Cerasuolo di Vittoria", "Frappato"),
    "sicilia": (ITALY, "Sicilia", None),
    "taurasi": (ITALY, "Taurasi", "Aglianico"),
    "vulture": (ITALY, "Aglianico del Vulture", "Aglianico"),
    "manduria": (ITALY, "Primitivo di Manduria", "Zinfandel"),
    "salice salentino": (ITALY, "Salice Salentino", "Negroamaro"),
    "abruzzo": (ITALY, "Abruzzo", "Montepulciano"),
    # A theme of just "Montepulciano" could mean the grape or the Tuscan town,
    # so this claims the country and nothing more.
    "montepulciano": (ITALY, None, None),
    "alto adige": (ITALY, "Alto Adige", None),
    "trentino": (ITALY, "Trentino", None),
    "collio": (ITALY, "Collio", None),
    "friuli": (ITALY, "Friuli", None),
    "franciacorta": (ITALY, "Franciacorta", "Chardonnay"),
    "prosecco": (ITALY, "Prosecco", "Glera"),
    "valdobbiadene": (ITALY, "Valdobbiadene", "Glera"),
    "gavi": (ITALY, "Gavi", "Cortese"),
    "asti": (ITALY, "Asti", None),
    "alba": (ITALY, "Alba", None),
    "piemonte": (ITALY, "Piemonte", None),
    # Italian legal designations: they only appear on Italian labels.
    "docg": (ITALY, None, None),
    "igt": (ITALY, None, None),
    "umbria": (ITALY, "Umbria", None),
    "lazio": (ITALY, "Lazio", None),
    "puglia": (ITALY, "Puglia", None),
    "veneto": (ITALY, "Veneto", None),
    # -- Spain ------------------------------------------------------------
    "rioja": (SPAIN, "Rioja", "Tempranillo"),
    "ribera del duero": (SPAIN, "Ribera del Duero", "Tempranillo"),
    "priorat": (SPAIN, "Priorat", "Grenache"),
    "montsant": (SPAIN, "Montsant", "Grenache"),
    "bierzo": (SPAIN, "Bierzo", "Mencía"),
    "rueda": (SPAIN, "Rueda", "Verdejo"),
    "rias baixas": (SPAIN, "Rías Baixas", "Albariño"),
    "gredos": (SPAIN, "Sierra de Gredos", "Grenache"),
    "granada": (SPAIN, "Granada", None),
    "valdeorras": (SPAIN, "Valdeorras", "Godello"),
    "ribeira sacra": (SPAIN, "Ribeira Sacra", "Mencía"),
    "jumilla": (SPAIN, "Jumilla", "Mourvèdre"),
    "yecla": (SPAIN, "Yecla", "Mourvèdre"),
    "somontano": (SPAIN, "Somontano", None),
    "navarra": (SPAIN, "Navarra", None),
    "penedes": (SPAIN, "Penedès", None),
    "cava": (SPAIN, "Cava", None),
    "jerez": (SPAIN, "Jerez", "Palomino"),
    "sherry": (SPAIN, "Jerez", "Palomino"),
    "castilla y leon": (SPAIN, "Castilla y León", None),
    "toro": (SPAIN, "Toro", "Tempranillo"),
    # -- Portugal ---------------------------------------------------------
    "douro": (PORTUGAL, "Douro", PORT_BLEND),
    "porto": (PORTUGAL, "Porto", PORT_BLEND),
    "portvin": (PORTUGAL, "Porto", PORT_BLEND),
    "portwine": (PORTUGAL, "Porto", PORT_BLEND),
    "dao": (PORTUGAL, "Dão", None),
    "bairrada": (PORTUGAL, "Bairrada", "Baga"),
    "alentejo": (PORTUGAL, "Alentejo", None),
    "vinho verde": (PORTUGAL, "Vinho Verde", "Albariño"),
    "colares": (PORTUGAL, "Colares", "Ramisco"),
    "setubal": (PORTUGAL, "Setúbal", None),
    "madeira": (PORTUGAL, "Madeira", None),
    "acores": (PORTUGAL, "Azores", None),
    "azores": (PORTUGAL, "Azores", None),
    "bucelas": (PORTUGAL, "Bucelas", "Arinto"),
    "lisboa": (PORTUGAL, "Lisboa", None),
    # -- Germany, Austria, Hungary ----------------------------------------
    "mosel": (GERMANY, "Mosel", "Riesling"),
    "saar": (GERMANY, "Saar", "Riesling"),
    "rheingau": (GERMANY, "Rheingau", "Riesling"),
    "nahe": (GERMANY, "Nahe", "Riesling"),
    "pfalz": (GERMANY, "Pfalz", None),
    "rheinhessen": (GERMANY, "Rheinhessen", None),
    "baden": (GERMANY, "Baden", None),
    "franken": (GERMANY, "Franken", "Silvaner"),
    "wurttemberg": (GERMANY, "Württemberg", None),
    "wachau": (AUSTRIA, "Wachau", "Grüner Veltliner"),
    "kamptal": (AUSTRIA, "Kamptal", "Grüner Veltliner"),
    "kremstal": (AUSTRIA, "Kremstal", "Grüner Veltliner"),
    "burgenland": (AUSTRIA, "Burgenland", None),
    "leithaberg": (AUSTRIA, "Leithaberg", None),
    "neusiedlersee": (AUSTRIA, "Neusiedlersee", None),
    "weinviertel": (AUSTRIA, "Weinviertel", "Grüner Veltliner"),
    "steiermark": (AUSTRIA, "Steiermark", None),
    "schilcher": (AUSTRIA, "Steiermark", "Blauer Wildbacher"),
    "tokaj": (HUNGARY, "Tokaj", "Furmint"),
    "tokaji": (HUNGARY, "Tokaj", "Furmint"),
    "szekszard": (HUNGARY, "Szekszárd", None),
    "villany": (HUNGARY, "Villány", None),
    # -- Greece, Lebanon --------------------------------------------------
    "santorini": (GREECE, "Santorini", "Assyrtiko"),
    "nemea": (GREECE, "Nemea", "Agiorgitiko"),
    "naoussa": (GREECE, "Naoussa", "Xinomavro"),
    "noussa": (GREECE, "Naoussa", "Xinomavro"),  # spelled thus in the sheet
    "musar": (LEBANON, "Bekaa Valley", None),
    "hochar": (LEBANON, "Bekaa Valley", None),  # Château Musar's second label
    "bekaa": (LEBANON, "Bekaa Valley", None),
    # -- United States ----------------------------------------------------
    "napa": (USA, "Napa Valley", None),
    "oakville": (USA, "Oakville", "Cabernet Sauvignon"),
    "rutherford": (USA, "Rutherford", "Cabernet Sauvignon"),
    "stags leap": (USA, "Stags Leap", "Cabernet Sauvignon"),
    "howell mountain": (USA, "Howell Mountain", "Cabernet Sauvignon"),
    "sonoma": (USA, "Sonoma", None),
    "russian river": (USA, "Russian River Valley", None),
    "alexander valley": (USA, "Alexander Valley", None),
    "dry creek": (USA, "Dry Creek Valley", "Zinfandel"),
    "geyserville": (USA, "Alexander Valley", "Zinfandel"),
    "lytton springs": (USA, "Dry Creek Valley", "Zinfandel"),
    "anderson valley": (USA, "Anderson Valley", None),
    "mendocino": (USA, "Mendocino", None),
    "paso robles": (USA, "Paso Robles", None),
    "santa barbara": (USA, "Santa Barbara", None),
    "santa rita": (USA, "Sta. Rita Hills", "Pinot Noir"),
    "sta rita": (USA, "Sta. Rita Hills", "Pinot Noir"),
    "santa lucia": (USA, "Santa Lucia Highlands", "Pinot Noir"),
    "santa maria": (USA, "Santa Maria Valley", None),
    "edna valley": (USA, "Edna Valley", None),
    "monterey": (USA, "Monterey", None),
    "willamette": (USA, "Willamette Valley", "Pinot Noir"),
    "dundee hills": (USA, "Dundee Hills", "Pinot Noir"),
    "columbia valley": (USA, "Columbia Valley", None),
    "walla walla": (USA, "Walla Walla", None),
    "finger lakes": (USA, "Finger Lakes", "Riesling"),
    "california": (USA, "California", None),
    "oregon": (USA, "Oregon", None),
    # -- South America ----------------------------------------------------
    "maipo": (CHILE, "Maipo Valley", None),
    "colchagua": (CHILE, "Colchagua Valley", None),
    "casablanca": (CHILE, "Casablanca Valley", None),
    "aconcagua": (CHILE, "Aconcagua", None),
    "rapel": (CHILE, "Rapel Valley", None),
    "curico": (CHILE, "Curicó Valley", None),
    "limari": (CHILE, "Limarí Valley", None),
    "itata": (CHILE, "Itata Valley", None),
    "mendoza": (ARGENTINA, "Mendoza", "Malbec"),
    "uco": (ARGENTINA, "Uco Valley", "Malbec"),
    "salta": (ARGENTINA, "Salta", "Malbec"),
    "cafayate": (ARGENTINA, "Cafayate", "Malbec"),
    "patagonia": (ARGENTINA, "Patagonia", None),
    # -- Australia, New Zealand -------------------------------------------
    "barossa": (AUSTRALIA, "Barossa Valley", "Syrah"),
    "mclaren vale": (AUSTRALIA, "McLaren Vale", "Syrah"),
    "clare valley": (AUSTRALIA, "Clare Valley", "Riesling"),
    "eden valley": (AUSTRALIA, "Eden Valley", "Riesling"),
    "coonawarra": (AUSTRALIA, "Coonawarra", "Cabernet Sauvignon"),
    "yarra": (AUSTRALIA, "Yarra Valley", None),
    "margaret river": (AUSTRALIA, "Margaret River", None),
    "hunter valley": (AUSTRALIA, "Hunter Valley", None),
    "adelaide hills": (AUSTRALIA, "Adelaide Hills", None),
    "heathcote": (AUSTRALIA, "Heathcote", "Syrah"),
    "tasmania": (AUSTRALIA, "Tasmania", None),
    "marlborough": (NZ, "Marlborough", "Sauvignon Blanc"),
    "central otago": (NZ, "Central Otago", "Pinot Noir"),
    "hawke": (NZ, "Hawke's Bay", None),
    "martinborough": (NZ, "Martinborough", "Pinot Noir"),
    "gisborne": (NZ, "Gisborne", None),
    # -- South Africa -----------------------------------------------------
    "stellenbosch": (SOUTH_AFRICA, "Stellenbosch", None),
    "swartland": (SOUTH_AFRICA, "Swartland", None),
    "walker bay": (SOUTH_AFRICA, "Walker Bay", None),
    "hemel": (SOUTH_AFRICA, "Hemel-en-Aarde", None),
    "franschhoek": (SOUTH_AFRICA, "Franschhoek", None),
    "constantia": (SOUTH_AFRICA, "Constantia", None),
    "paarl": (SOUTH_AFRICA, "Paarl", None),
    "elgin": (SOUTH_AFRICA, "Elgin", None),
    "robertson": (SOUTH_AFRICA, "Robertson", None),
}

# Country names as they appear in the sheet — Norwegian in brackets after a wine
# name, and in tasting themes.
COUNTRIES: dict[str, str] = {
    "frankrike": FRANCE, "france": FRANCE, "fransk": FRANCE, "frankr": FRANCE,
    "italia": ITALY, "italy": ITALY, "italiensk": ITALY,
    "spania": SPAIN, "spain": SPAIN, "spansk": SPAIN,
    "portugal": PORTUGAL, "portugisisk": PORTUGAL,
    "tyskland": GERMANY, "germany": GERMANY, "tysk": GERMANY,
    "osterrike": AUSTRIA, "austria": AUSTRIA, "osterriksk": AUSTRIA,
    "ungarn": HUNGARY, "hungary": HUNGARY,
    "hellas": GREECE, "greece": GREECE, "gresk": GREECE,
    "libanon": LEBANON, "lebanon": LEBANON,
    "usa": USA, "amerika": USA, "california": USA, "statene": USA,
    "chile": CHILE, "chilensk": CHILE,
    "argentina": ARGENTINA, "argentinsk": ARGENTINA,
    "australia": AUSTRALIA, "australsk": AUSTRALIA,
    "new zealand": NZ, "ny zealand": NZ, "newzealand": NZ,
    "sor afrika": SOUTH_AFRICA, "sorafrika": SOUTH_AFRICA,
    "south africa": SOUTH_AFRICA, "sorafrikans": SOUTH_AFRICA,
    "sorafrikansk": SOUTH_AFRICA,
    "uruguay": "Uruguay", "brasil": "Brazil", "brazil": "Brazil",
    "england": "England", "engelsk": "England", "storbritannia": "England",
    "belgia": "Belgium", "belgium": "Belgium",
    "danmark": "Denmark", "denmark": "Denmark",
    "israel": "Israel", "tyrkia": "Turkey", "turkey": "Turkey",
    "georgia": "Georgia", "sveits": "Switzerland", "slovenia": "Slovenia",
    "kroatia": "Croatia", "romania": "Romania", "bulgaria": "Bulgaria",
    "moldova": "Moldova", "canada": "Canada", "japan": "Japan",
    "kina": "China", "marokko": "Morocco", "libanesisk": LEBANON,
    "norge": "Norway", "sverige": "Sweden",
}

# Grapes grown so overwhelmingly in one country that the grape implies it. Used
# only as a last resort, when neither the name nor the theme names a place.
# Deliberately excludes the widely-planted ones: Tannat is Uruguay *and* Madiran,
# Albariño is Spain *and* Portugal, Syrah is everywhere.
GRAPE_HOMELAND: dict[str, str] = {
    "Nebbiolo": ITALY, "Sangiovese": ITALY, "Barbera": ITALY, "Dolcetto": ITALY,
    "Corvina": ITALY, "Aglianico": ITALY, "Nero d'Avola": ITALY,
    "Nerello Mascalese": ITALY, "Frappato": ITALY, "Garganega": ITALY,
    "Montepulciano": ITALY, "Negroamaro": ITALY, "Cortese": ITALY, "Glera": ITALY,
    "Lagrein": ITALY, "Teroldego": ITALY, "Schioppettino": ITALY,
    "Baga": PORTUGAL, "Ramisco": PORTUGAL, "Touriga Nacional": PORTUGAL,
    "Touriga Franca": PORTUGAL, "Arinto": PORTUGAL, "Fernão Pires": PORTUGAL,
    "Mencía": SPAIN, "Verdejo": SPAIN, "Godello": SPAIN, "Palomino": SPAIN,
    "Graciano": SPAIN, "Pedro Ximénez": SPAIN,
    "Assyrtiko": GREECE, "Xinomavro": GREECE, "Agiorgitiko": GREECE,
    "Furmint": HUNGARY,
    "Grüner Veltliner": AUSTRIA, "Blaufränkisch": AUSTRIA, "Zweigelt": AUSTRIA,
    "St. Laurent": AUSTRIA,
    "Pinotage": SOUTH_AFRICA,
    "Saperavi": "Georgia",
    "Melon de Bourgogne": FRANCE, "Savagnin": FRANCE, "Poulsard": FRANCE,
    "Trousseau": FRANCE, "Mondeuse": FRANCE,
}

# Not wine. These get a country but must never get a grape.
NON_WINE = ("trappist", "beer geek", "mikkeller", "brewing", " ipa ", " stout ")

# Tasting-order labels the spreadsheet puts in front of a name: "1. ", "10) ",
# and the blind-tasting "Vin 3: " that follows it.
ORDER_PREFIX = re.compile(r"^\s*\d{1,2}\s*[.)]\s*(?:vin\s*\d{1,2}\s*[:.]?\s*)?", re.IGNORECASE)


def clean_name(name: str) -> str:
    """Drop the pouring-order label from the front of a wine's name.

    Only the mechanical `1.` / `10)` / `Vin 3:` forms. Names like
    "3. Lennart 1 Toscana Il Poggione Brunello" keep everything after the
    number, because the rest is content — who brought it and where it's from —
    not noise.
    """
    return ORDER_PREFIX.sub("", name or "").strip()


def parse_vintage(name: str, *, latest: int = 2026) -> int | None:
    """The vintage from a wine's name, if it states one unambiguously.

    Four-digit years only. Two-digit ones ("Protos Reserva 09") are left alone:
    the sheet also writes prices, cru levels and blend percentages as bare
    numbers, so guessing would be wrong often enough to matter.
    """
    years = [int(match) for match in re.findall(r"\b(1[89]\d\d|20\d\d)\b", name or "")]
    plausible = [year for year in years if 1850 <= year <= latest]
    return max(plausible) if plausible else None


def _matches(text: str, table: dict) -> list[tuple[str, object]]:
    """Every key present in the folded text, longest first."""
    found = [(key, value) for key, value in table.items() if f" {key} " in text]
    return sorted(found, key=lambda pair: -len(pair[0]))


def _find(text: str, table: dict) -> tuple[str, object] | None:
    """Longest matching key wins, so Brunello di Montalcino beats Montalcino."""
    found = _matches(text, table)
    return found[0] if found else None


def _one_country(text: str) -> str | None:
    """A country, but only when the text names exactly one.

    A theme like "Argentina vs Chile" names two, and picking either would be a
    coin toss recorded as fact — which is how a Chilean Cabernet ends up filed
    under Argentina.
    """
    names = {value for _, value in _matches(text, COUNTRIES)}
    return next(iter(names)) if len(names) == 1 else None


def is_non_wine(name: str) -> bool:
    folded = fold(name)
    return any(marker in folded for marker in NON_WINE)


def derive(name: str, theme: str | None = None) -> Origin:
    """Country, region and grape for a wine, from its name and its tasting.

    The name is trusted over the theme: a Bordeaux-themed evening can still hold
    a ringer from elsewhere, and several of these tastings deliberately did.

    A theme that names two places — "Tokaji & Toscana", "Argentina vs Chile" —
    is not allowed to settle anything, since choosing one of the two would be a
    guess written down as a fact.
    """
    country = region = grape = None

    for position, text in enumerate((fold(name), fold(theme or ""))):
        from_theme = position == 1
        appellation = _find(text, APPELLATIONS)
        if from_theme and appellation:
            places = {value for _, value in _matches(text, APPELLATIONS)}
            if len({place[0] for place in places}) > 1:
                appellation = None  # the theme spans countries
        if appellation and not region:
            found_country, found_region, found_grape = appellation[1]
            country = country or found_country
            region = found_region
            grape = grape or found_grape

        found_grape = _find(text, GRAPES)
        if found_grape and not grape:
            grape = found_grape[1]

        named_country = _one_country(text)
        if named_country and not country:
            country = named_country

    if is_non_wine(name):
        grape = None
    if country is None and grape:
        country = GRAPE_HOMELAND.get(grape)

    return Origin(country=country, region=region, grape=grape)
