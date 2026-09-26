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

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from bot.db import utcnow_iso

from .. import label as label_reader
from .. import queries
from ..deps import Cfg, Db, LoggedIn, Member, page

router = APIRouter(prefix="/events")

# What the club plans. `bot/db.py` documents what each means; the labels are
# what the dropdown shows.
KINDS = {
    "tasting": "Tasting",
    "blind": "Blind tasting",
    "trip": "Away trip",
    "other": "Something else",
}
POURING = ("tasting", "blind")      # the kinds that have bottles


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
        kinds=KINDS,
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
    kind: Annotated[str, Form()] = "tasting",
    theme: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "",
    ends_at: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    host: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    if kind not in KINDS:
        return _listing(request, db, error="That isn't a kind of event.", status=400)
    if not theme.strip():
        return _listing(request, db, error="It needs a name.", status=400)
    when = _to_utc(starts_at, cfg.tz)
    if when is None:
        return _listing(request, db, error="That isn't a date and time.", status=400)
    # A trip with no country cannot be filed in the archive later, and finding
    # that out months afterwards is no use to anyone.
    if kind == "trip" and not country.strip():
        return _listing(request, db, error="A trip needs a country.", status=400)
    until = _to_utc(ends_at, cfg.tz) if ends_at.strip() else None
    if ends_at.strip() and until is None:
        return _listing(request, db, error="That isn't an end date.", status=400)
    db.create_event(
        kind=kind,
        theme=theme.strip(),
        starts_at=when,
        ends_at=until,
        location=_clean(location),
        host=_clean(host),
        country=_clean(country),
        notes=_clean(notes),
        created_by=member[1] if member else None,
    )
    return RedirectResponse("/events", status_code=303)


def _detail(
    request: Request,
    db: Db,
    cfg: Cfg,
    row,
    *,
    error: str | None = None,
    prefill: dict | None = None,
    status: int = 200,
):
    """The event's own page. Shared, so a refused bottle comes back on it."""
    return page(
        request,
        "events/detail.html",
        event=row,
        wines=queries.event_wines(db, row["id"]),
        now=utcnow_iso(),
        kinds=KINDS,
        pours=row["kind"] in POURING,
        error=error,
        # What a photographed label read as, waiting to be checked. Empty the
        # rest of the time, which is what an untouched form is.
        prefill=prefill or {},
        label_reading=cfg.has_label_reading,
        status_code=status,
    )


@router.get("/{event_id}")
async def detail(request: Request, db: Db, cfg: Cfg, _: LoggedIn, event_id: int):
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    return _detail(request, db, cfg, row)


@router.post("/{event_id}")
async def edit(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    kind: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "",
    ends_at: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    host: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    if queries.event(db, event_id) is None:
        raise HTTPException(404, "No such event")
    if kind and kind not in KINDS:
        raise HTTPException(400, "That isn't a kind of event.")
    when = _to_utc(starts_at, cfg.tz) if starts_at.strip() else None
    if starts_at.strip() and when is None:
        raise HTTPException(400, "That isn't a date and time.")
    until = _to_utc(ends_at, cfg.tz) if ends_at.strip() else None
    db.update_event(
        event_id,
        kind=kind or None,
        theme=theme.strip() or None,
        starts_at=when,
        ends_at=until,
        location=location.strip(),
        host=host.strip(),
        country=country.strip(),
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
    cfg: Cfg,
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
        return _detail(request, db, cfg, row, error="A bottle needs a name.", status=400)
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


@router.post("/{event_id}/wines/label")
async def read_bottle_label(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    photo: Annotated[UploadFile | None, File()] = None,
):
    """Read a photographed label and come back with the bottle form filled in.

    It writes nothing. The model is good at labels and still wrong sometimes, so
    what it reads lands in the form beside the Add button and a person presses
    it — the same form, and the same write, as typing the bottle in by hand.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if not cfg.has_label_reading:
        raise HTTPException(404, "No label reader configured")
    if photo is None or not photo.filename:
        return _detail(request, db, cfg, row, error="No photo came through.", status=400)
    # Both checked before anything is sent, so a video picked by mistake is
    # refused here rather than after megabytes have moved. `read_label` checks
    # them again for its own sake; this is the one that saves the call.
    if (photo.size or 0) > label_reader.MAX_BYTES:
        return _detail(
            request, db, cfg, row,
            error="That photo is too big — try again, or type it in.", status=400,
        )
    if photo.content_type not in label_reader.ALLOWED_TYPES:
        return _detail(
            request, db, cfg, row,
            error="That isn't a photo. Pick an image, or type it in.", status=400,
        )
    try:
        image = await photo.read()
        # A few seconds on the wire, and this is the one blocking call in the
        # app — off the event loop so the rest of the page-serving carries on.
        found = await run_in_threadpool(
            label_reader.read_label, image, photo.content_type or "", cfg.anthropic_key
        )
    except label_reader.LabelUnreadable as exc:
        return _detail(request, db, cfg, row, error=str(exc), status=400)
    finally:
        await photo.close()
    return _detail(
        request, db, cfg, row,
        prefill={k: v for k, v in found.model_dump().items() if v is not None},
    )


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
