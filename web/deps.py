"""Shared plumbing: the database handle, the templates, and the login gate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.config import Config
from bot.db import Database

from . import auth
from .avatars import colour, initials
from .queries import IndexCache
from .search import Index

WEB_ROOT = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


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


templates.env.filters["score"] = fmt_score
templates.env.globals["fmt_month"] = fmt_month
templates.env.globals["initials"] = initials
templates.env.globals["avatar_colour"] = colour


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
    """Gate every page except /login — this is nine people's private notes."""
    if not auth.is_signed_in(request):
        raise LoginRequired(request.url.path)


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
Db = Annotated[Database, Depends(get_db)]
Cfg = Annotated[Config, Depends(get_config)]
WineIndex = Annotated[Index, Depends(get_index)]
LoggedIn = Annotated[None, Depends(require_login)]
