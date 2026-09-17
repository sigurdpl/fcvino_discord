"""Shared plumbing: the database handle, the templates, and the login gate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.config import Config
from bot.db import Database, parse_utc

from . import access, auth
from .avatars import colour, initials
from .queries import IndexCache
from .search import Index

WEB_ROOT = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def static_url(name: str) -> str:
    """`/static/<name>` with a version stamp taken from the file itself.

    The markup and the stylesheet are deployed together but cached apart, so a
    browser — or Cloudflare, which sits in front of the tunnel — can hold an old
    stylesheet against new HTML. That is not a cosmetic mismatch: it has already
    produced a page with the wordmark drawn twice and four images collapsed to
    nothing, because each of those depends on a rule the old sheet lacked.

    Stamping the URL means a changed file is a different URL, so no cache can
    serve the stale one. Read per render rather than at startup, because
    `--reload` restarts on Python changes and not on CSS ones.
    """
    path = WEB_ROOT / "static" / name
    try:
        stamp = f"{path.stat().st_mtime_ns:x}"[-8:]
    except OSError:
        return f"/static/{name}"      # absent is normal: the club's pictures
    return f"/static/{name}?v={stamp}"


def fmt_score(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def fmt_month(year: int | None, month: int | None) -> str:
    """'March 2016', or just the year when the sheet never recorded a month."""
    if year is None:
        return "—"
    if not month:
        return str(year)
    names = ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December")
    return f"{names[month - 1]} {year}"


def fmt_when(stamp: str | None, tz: str = "Europe/Oslo") -> str:
    """A stored UTC instant as the evening it is locally: 'Sat 12 Oct, 19:00'.

    Instants are stored in UTC throughout, as `bot/db.py` says; the club reads
    them in its own time, which is the same split `bot/formatting.local_day`
    already makes for kickoffs.
    """
    if not stamp:
        return "—"
    try:
        when = parse_utc(stamp).astimezone(ZoneInfo(tz))
    except (ValueError, ZoneInfoNotFoundError):
        return stamp
    return f"{when:%a} {when.day} {when:%B}, {when:%H:%M}"


def fmt_local_input(stamp: str | None, tz: str = "Europe/Oslo") -> str:
    """The same instant as a <input type="datetime-local"> wants it."""
    if not stamp:
        return ""
    try:
        return parse_utc(stamp).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        return ""


templates.env.filters["score"] = fmt_score
templates.env.globals["fmt_month"] = fmt_month
templates.env.globals["fmt_when"] = fmt_when
templates.env.globals["fmt_local_input"] = fmt_local_input
templates.env.globals["initials"] = initials
templates.env.globals["avatar_colour"] = colour
templates.env.globals["static"] = static_url


class LoginRequired(Exception):
    """Raised by `require_login`; turned into a redirect by the app's handler."""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def get_config(request: Request) -> Config:
    return request.app.state.cfg


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_index(request: Request) -> Index:
    cache: IndexCache = request.app.state.index
    return cache.get(request.app.state.db)


def require_login(request: Request) -> None:
    """Gate every page except /login — this is nine people's private notes.

    Cloudflare Access, where it is in front of us, has already established who
    this is by email; that counts as signing in, and the club password is only
    asked for when it hasn't. See `web/access.py` for why the header is trusted.
    """
    if auth.is_signed_in(request):
        return
    if _sign_in_via_access(request):
        return
    raise LoginRequired(request.url.path)


def _sign_in_via_access(request: Request) -> bool:
    """Sign in — and claim a name where the address maps to one — from Access."""
    email = access.identity(request, get_config(request))
    if email is None:
        return False
    auth.sign_in(request)
    members = _members(request)
    name = access.member_name(email, get_config(request), [m["name"] for m in members])
    # Only a name the club's records already know: a hand-written .env mapping
    # can name anyone at all, and a typo there should leave you unnamed rather
    # than invent an eleventh member.
    match = next((m for m in members if m["name"] == name), None)
    if match is not None:
        auth.claim_member(request, match["id"], match["name"])
    return True


def redirect_to_login(request: Request, next_url: str) -> RedirectResponse:
    request.session[auth.NEXT_URL] = next_url
    return RedirectResponse("/login", status_code=303)


def page(request: Request, template: str, *, status_code: int = 200, **context):
    """Render a template with the things every page's chrome needs."""
    member = auth.current_member(request)
    # The chrome in base.html needs these on every page whether the route asked
    # for them or not: the member list for the "who are you?" prompt, and the
    # club's headline numbers for the group header — which should read the same
    # everywhere, the way a group's member count does.
    context.setdefault("members", _members(request))
    context.setdefault("totals", _totals(request))
    return templates.TemplateResponse(
        request,
        template,
        {
            "member": member,
            "member_name": member[1] if member else None,
            **context,
        },
        status_code=status_code,
    )


def _members(request: Request) -> list:
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    return db.query("SELECT id, name FROM wine_members ORDER BY name")


def _totals(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return None
    from .queries import cellar_totals
    return cellar_totals(db)


def safe_path(url: str | None, host: str | None = None, fallback: str = "/wine") -> str:
    """The path part of a URL, but only when it points back at this site.

    Used for the "return where you came from" redirects after signing in or
    claiming a name. Taking the path alone would already keep the visitor on
    this site, but it would happily turn `https://example.com/evil` into
    `/evil` — following a stranger's link to a page of our choosing. So an
    absolute URL has to name this host, or it is ignored entirely.
    """
    if not url:
        return fallback
    parts = urlsplit(url)
    if parts.netloc and parts.netloc != host:
        return fallback
    path = parts.path or fallback
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    return path


# Annotated dependencies rather than `= Depends(...)` defaults: the same wiring,
# but it reads as a type and keeps a function call out of a default argument.
def get_member(
    request: Request, _: Annotated[None, Depends(require_login)]
) -> tuple[int, str] | None:
    """Who this session says it is, or None — being signed in does not require
    having said, so a handler recording an author must cope with both.

    It depends on the gate rather than sitting beside it because behind
    Cloudflare Access the gate is also what *claims* the name, from the vouched
    address. Read in parallel with it, the session's very first request would
    find nobody there and file the evening under no one.
    """
    return auth.current_member(request)


Db = Annotated[Database, Depends(get_db)]
Cfg = Annotated[Config, Depends(get_config)]
WineIndex = Annotated[Index, Depends(get_index)]
LoggedIn = Annotated[None, Depends(require_login)]
Member = Annotated["tuple[int, str] | None", Depends(get_member)]
