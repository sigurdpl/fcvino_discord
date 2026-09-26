"""Finding a bottle in Vinmonopolet's catalogue, for its label picture.

The club buys there, so their catalogue and this cellar overlap by design —
which is what makes a free source of label photographs possible at all. Two
open endpoints, neither needing a key:

    search  www.vinmonopolet.no/vmpws/v2/vmp/products/search?q=…&fields=FULL
    images  bilder.vinmonopolet.no/cache/{W}x{H}-0/{code}-1.jpg

The search answers with the product number, the name, the country and district
— and the image URLs already built. So the only real work is deciding whether
their wine is our wine, which is what most of this module is about.

**What it cannot do.** Vinmonopolet sells the vintage they have now, so a match
is often a different year than the bottle the club drank: their Bussola
Valpolicella is 2020 where ours is 2010. The label is usually the same picture,
but it is not the same bottle, and `matched_vintage` says when they differ so a
page can be honest about it.

Nothing here writes to the database or the network without being asked: the
fetcher is injected, so the tests never touch Vinmonopolet.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from bot.wine_origin import GRAPES, _find, fold

SEARCH = "https://www.vinmonopolet.no/vmpws/v2/vmp/products/search"
IMAGE = "https://bilder.vinmonopolet.no/cache/{size}x{size}-0/{code}-1.jpg"

# Said plainly, so anyone reading their logs can see who this is and why.
USER_AGENT = "FCVino/1.0 (nine-person wine club archive; label pictures only)"

# One request a second. This asks a shop's website about 1172 bottles once,
# not continuously, and there is no reason to be in a hurry about it.
PAUSE = 1.0

VINTAGE = re.compile(r"\b(19|20)\d{2}\b")
PARENTHETICAL = re.compile(r"\([^)]*\)")          # (330 kr), (Morten)
BLEND_NOTE = re.compile(r"\b\d{1,3}\s*%[^,]*")    # 98% Cab, 2% Cab Franc


@dataclass(frozen=True)
class Match:
    """One Vinmonopolet product, and how sure we are it is ours."""

    code: str
    name: str
    country: str | None
    district: str | None
    vintage: int | None
    score: float

    def image(self, size: int = 300) -> str:
        return IMAGE.format(size=size, code=self.code)


def searchable(name: str) -> str:
    """The name as a question Vinmonopolet can answer.

    The club's older names carry the price and the blend inside them —
    `Terrazas Afincado Malbec 2008 (330 kr)`, `Don Melchor 2007 98% Cab, 2% Cab
    Franc` — and those turn a findable wine into nothing at all. The vintage
    goes too: they stock the year they have, not the year we drank.

    Stripping these took the hit rate on a sample of the cellar from 6/15 to
    11/15, which is the whole reason this function exists.
    """
    s = PARENTHETICAL.sub(" ", name)
    s = BLEND_NOTE.sub(" ", s)
    s = VINTAGE.sub(" ", s)
    # A blend note stops at its comma, so stripping "98% Cab, 2% Cab Franc"
    # leaves the comma behind — and `Don Melchor ,` is a worse question than
    # `Don Melchor`.
    s = re.sub(r"[,;/&+]+", " ", s)
    return " ".join(s.split()).strip(" -–—")


def vintage_of(name: str) -> int | None:
    found = VINTAGE.search(name)
    return int(found.group(0)) if found else None


def confidence(ours: str, theirs: str) -> float:
    """How much of our name their name accounts for, 0 to 1.

    Token overlap rather than anything cleverer, because the failure that
    matters is hanging the wrong label on a bottle — and the cheapest guard
    against it is insisting that most of what we call the wine appears in what
    they call it. The producer usually leads both names, so a wine that shares
    only its grape ("Malbec") scores low and is rejected.
    """
    mine = [w for w in fold(searchable(ours)).split() if len(w) > 1]
    if not mine:
        return 0.0
    yours = set(fold(theirs).split())
    return sum(w in yours for w in mine) / len(mine)


Fetcher = Callable[[str], dict]


def _fetch(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def search(term: str, *, fetch: Fetcher = _fetch, limit: int = 5) -> list[dict]:
    """Raw products for a term, newest API shape, or [] when there are none."""
    if not term.strip():
        return []
    url = (f"{SEARCH}?q={urllib.parse.quote(term)}"
           f"&pageSize={limit}&fields=FULL")
    try:
        return fetch(url).get("products", []) or []
    except Exception:
        # A shop's website being slow or cross is not a reason to stop a run
        # over a thousand bottles. The caller reports what was missed.
        return []


def same_producer(ours: str, theirs: str) -> bool:
    """Does their name carry ours' first real word — usually the estate?

    Overlap alone is not enough. `Anselma Barolo Vigna Rionda` shares three
    words out of four with `Massolino Barolo Vigna Rionda Riserva`, and they
    are different growers' wines from the same vineyard. The name that decides
    it is the one in front.
    """
    mine = [w for w in fold(searchable(ours)).split() if len(w) > 2]
    return bool(mine) and mine[0] in fold(theirs)


def grapes_clash(ours: str, theirs: str) -> bool:
    """Do both names name a grape, and are they different ones?

    `Chiara Boschis Dolcetto d'Alba` against their `Chiara Boschis Barbera
    d'Alba` is the same grower, the same village and the wrong bottle. Reusing
    the table `wine_origin` already keeps, so the two agree about what a grape
    is called.
    """
    mine = _find(fold(ours), GRAPES)
    yours = _find(fold(theirs), GRAPES)
    return bool(mine and yours and mine[1] != yours[1])


def best_match(name: str, *, fetch: Fetcher = _fetch, floor: float = 0.6) -> Match | None:
    """Their wine for our name, or None when nothing is convincing enough.

    Three hurdles, because the failure that matters is a stranger's label on
    the club's bottle: enough of our words in theirs, the same producer, and no
    contradicting grape. A missed picture costs nothing; a wrong one is a lie
    in an archive that is meant to outlive the laptop.
    """
    best: Match | None = None
    for product in search(searchable(name), fetch=fetch):
        theirs = product.get("name") or ""
        score = confidence(name, theirs)
        if score < floor or (best and score <= best.score):
            continue
        if not same_producer(name, theirs) or grapes_clash(name, theirs):
            continue
        best = Match(
            code=str(product.get("code") or ""),
            name=theirs,
            country=(product.get("main_country") or {}).get("name"),
            district=(product.get("district") or {}).get("name"),
            vintage=vintage_of(theirs),
            score=score,
        )
    return best if best and best.code else None
