"""The three things that make the site an icon on a phone rather than a URL.

Adding it to the home screen is as close to an app as nine people need: an
icon, a full screen and a session that outlives a browser tab. What that takes
is a manifest, a service worker and somewhere to land when nothing can be
reached.

**The manifest is built rather than stored**, for one reason. The icons are
made from the club's wordmark, and `.gitignore` keeps the club's pictures out
of a public repository — so a fresh checkout, and the Raspberry Pi on the day
it is set up, will not have them. A manifest naming an icon that is not there
is the worst of both: Android declines to install and says nothing, anywhere,
about why. So the manifest names the icons that exist, and `missing_icons()`
lets the app say so at startup instead of leaving it silent.

Neither the worker nor the offline page asks for a login, on purpose. The
worker is fetched by the browser rather than by a page, and the offline page's
whole job is to be shown when nothing can be reached — including, in principle,
whatever decides who you are. Neither gives anything away: one is a script that
ships in the repository, the other says the club's archive is not answering.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..deps import WEB_ROOT, templates

router = APIRouter()

STATIC = WEB_ROOT / "static"

# What a launcher draws. 192 and 512 are the two Android asks for; 180 is
# iOS's, which is linked from the page rather than named here because iOS
# ignores the manifest's icons entirely.
ICONS = ((192, "icon-192.png"), (512, "icon-512.png"))
APPLE_ICON = "icon-180.png"

# The page colour, `--page` in style_dark2.css. The phone paints the status bar
# and the splash screen with it, so a wrong value here is visible on every
# launch — which is what `test_the_theme_colour_matches_the_page` is for.
THEME = "#0a0b0a"


def missing_icons() -> list[str]:
    """Which home-screen icons this checkout has not got, for the startup log."""
    wanted = [APPLE_ICON, *(name for _, name in ICONS)]
    return [name for name in wanted if not (STATIC / name).exists()]


def _icons() -> list[dict]:
    """Only the icons actually on disk, each declared twice.

    `any` is drawn as given; `maskable` may be cropped to whatever shape the
    launcher likes, usually a circle. The same file serves both because it is
    padded to keep the wordmark inside the central 80% — the safe zone — which
    is the whole reason it is a square of burgundy rather than a tight crop.
    """
    found = [(size, name) for size, name in ICONS if (STATIC / name).exists()]
    return [
        {"src": f"/static/{name}", "sizes": f"{size}x{size}",
         "type": "image/png", "purpose": purpose}
        for purpose in ("any", "maskable")
        for size, name in found
    ]


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """What the browser reads to decide this is an app rather than a page.

    Served as a route, not a file, so the icon list can tell the truth about
    what is on disk. The media type has to be right — anything else and every
    browser ignores the file without comment.
    """
    return JSONResponse(
        {
            "name": "FC Vino",
            "short_name": "FC Vino",
            "description": "The club's cellar, its evenings and its away trips.",
            "lang": "en",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": THEME,
            "theme_color": THEME,
            "icons": _icons(),
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """The worker, served from the root because that is the only place it works.

    A service worker controls its own directory and below, so the same file
    under `/static` would control `/static` and nothing else — it would install
    without complaint and then do nothing at all, which is the sort of bug that
    survives for months. Hence a route rather than the static mount.

    `no-cache` because a stale worker is the one cached thing that can outlive
    every other fix: browsers re-check it, but only if allowed to.
    """
    return FileResponse(
        STATIC / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/offline", include_in_schema=False)
async def offline(request: Request):
    """Shown by the worker when the club's server cannot be reached.

    Its own page rather than `base.html`: it is cached when the worker installs
    and served when nothing is reachable, so it must not want a database, a
    session, a stylesheet or anything else that would have to be fetched. That
    is why the little styling it has is written into it.
    """
    return templates.TemplateResponse(request, "offline.html")
