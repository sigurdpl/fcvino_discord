"""The evenings we have not held yet: plan one, line up its bottles, hold it.

The first part of the web app that writes. Plain forms rather than htmx, and a
303 after every POST, so a reload never offers to submit an evening twice.

The schema belongs to `bot/db.py`, as the rest of the app's does, so the writes
here are its named methods and the reads are `web/queries.py` — which keeps its
promise not to write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from bot.db import utcnow_iso

from .. import queries
from ..deps import Cfg, Db, LoggedIn, Member, page

router = APIRouter(prefix="/events")


def _clean(value: str | None) -> str | None:
    """The codebase's reading of a blank form field: absent, not empty."""
    return (value or "").strip() or None


def _number(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _to_utc(local: str, tz: ZoneInfo) -> str | None:
    """A `datetime-local` value as the UTC instant the database stores.

    The club types the evening in its own time; every instant in the database is
    UTC. Returns None when the browser sends something unparseable, which is the
    caller's cue to re-render rather than write.
    """
    try:
        return (
            datetime.fromisoformat(local.strip())
            .replace(tzinfo=tz)
            .astimezone(ZoneInfo("UTC"))
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return None


def _listing(request: Request, db: Db, *, error: str | None = None, status: int = 200):
    """The page itself. Shared so a rejected form comes back with its own page."""
    now = utcnow_iso()
    rows = queries.events(db)
    return page(
        request,
        "events/index.html",
        upcoming=[e for e in rows if e["starts_at"] >= now],
        past=[e for e in reversed(rows) if e["starts_at"] < now],
        error=error,
        status_code=status,
    )


@router.get("")
async def index(request: Request, db: Db, _: LoggedIn):
    return _listing(request, db)


@router.post("")
async def create(
    request: Request,
    db: Db,
    cfg: Cfg,
    member: Member,
    _: LoggedIn,
    theme: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    host: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    if not theme.strip():
        return _listing(request, db, error="An evening needs a theme.", status=400)
    when = _to_utc(starts_at, cfg.tz)
    if when is None:
        return _listing(request, db, error="That isn't a date and time.", status=400)
    db.create_event(
        theme=theme.strip(),
        starts_at=when,
        location=_clean(location),
        host=_clean(host),
        notes=_clean(notes),
        created_by=member[1] if member else None,
    )
    return RedirectResponse("/events", status_code=303)


@router.get("/{event_id}")
async def detail(request: Request, db: Db, _: LoggedIn, event_id: int):
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    return page(
        request,
        "events/detail.html",
        event=row,
        wines=queries.event_wines(db, event_id),
        now=utcnow_iso(),
        error=None,
    )


@router.post("/{event_id}")
async def edit(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    theme: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    host: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    if queries.event(db, event_id) is None:
        raise HTTPException(404, "No such event")
    when = _to_utc(starts_at, cfg.tz) if starts_at.strip() else None
    if starts_at.strip() and when is None:
        raise HTTPException(400, "That isn't a date and time.")
    db.update_event(
        event_id,
        theme=theme.strip() or None,
        starts_at=when,
        location=location.strip(),
        host=host.strip(),
        notes=notes.strip(),
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/delete")
async def remove(request: Request, db: Db, _: LoggedIn, event_id: int):
    db.delete_event(event_id)
    return RedirectResponse("/events", status_code=303)


@router.post("/{event_id}/wines")
async def add_wine(
    request: Request,
    db: Db,
    _: LoggedIn,
    event_id: int,
    name: Annotated[str, Form()] = "",
    producer: Annotated[str, Form()] = "",
    vintage: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    grape: Annotated[str, Form()] = "",
    price_nok: Annotated[str, Form()] = "",
    brought_by: Annotated[str, Form()] = "",
):
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if not name.strip():
        return page(
            request,
            "events/detail.html",
            event=row,
            wines=queries.event_wines(db, event_id),
            now=utcnow_iso(),
            error="A bottle needs a name.",
            status_code=400,
        )
    db.add_event_wine(
        event_id,
        name=name.strip(),
        producer=_clean(producer),
        vintage=_number(vintage),
        country=_clean(country),
        region=_clean(region),
        grape=_clean(grape),
        price_nok=_number(price_nok),
        brought_by=_clean(brought_by),
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/wines/{wine_id}/delete")
async def remove_wine(request: Request, db: Db, _: LoggedIn, event_id: int, wine_id: int):
    db.delete_event_wine(event_id, wine_id)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/promote")
async def promote(request: Request, db: Db, _: LoggedIn, event_id: int):
    """Move a held evening into the archive, where the club's history lives."""
    if queries.event(db, event_id) is None:
        raise HTTPException(404, "No such event")
    db.promote_event(event_id)
    return RedirectResponse(f"/events/{event_id}", status_code=303)
