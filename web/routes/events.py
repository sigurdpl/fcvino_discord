"""The evenings we have not held yet: plan one, line up its bottles, hold it.

The first part of the web app that writes. Plain forms rather than htmx, and a
303 after every POST, so a reload never offers to submit an evening twice.

The schema belongs to `bot/db.py`, as the rest of the app's does, so the writes
here are its named methods and the reads are `web/queries.py` — which keeps its
promise not to write.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from bot.db import parse_utc, utcnow_iso
from bot.wine_origin import fold

from .. import label as label_reader
from .. import queries
from ..auth import current_member
from ..deps import Cfg, Db, LoggedIn, Member, fmt_day, page

router = APIRouter(prefix="/events")

# What the club plans. `bot/db.py` documents what each means; the labels are
# what the dropdown shows.
# Two different blind evenings, and they are not the same shape. A `blind` has
# one person responsible who registers the bottles properly beforehand, and the
# page prints Wine 1 … Wine N until it closes. A `byo` is nine members with
# nine bottles nobody knows in advance: they go in as placeholders and are
# revealed one at a time during the evening, which is exactly when somebody
# needs to photograph a label and put the real name in.
KINDS = {
    "tasting": "Tasting",
    "blind": "Blind tasting",
    "byo": "Bring your own",
    "trip": "Away trip",
    "other": "Something else",
}
POURING = ("tasting", "blind", "byo")      # the kinds that have bottles

# The Robert Parker scale, as the club uses it. The dropdown stops at 50
# because nothing much below that gets poured twice; the typed box goes lower,
# since the club's own history holds 64 ratings under 50 and the table allows
# 1–100. 85 is where the list opens — the usual starting point, marked but not
# chosen, so a wine nobody got to records nothing rather than a score nobody gave.
SCALE = tuple(range(100, 49, -1))
USUAL = 85
LOWEST, HIGHEST = 1, 100


def _day_has_come(row, tz: ZoneInfo, now: datetime | None = None) -> bool:
    """Whether the evening's own day has arrived, in the club's timezone.

    An evening can be started on its day or later, never before: the club sits
    down at seven whatever the diary says, so running early on the night itself
    is normal, and an idle click on next January's row is not.

    Local *dates*, not instants. `starts_at` is stored in UTC, and an evening
    at 00:30 on the 7th in Oslo is 22:30 on the 6th in UTC — compare the stored
    values and that one opens a day early.

    An unreadable date says yes. It cannot happen, since the column is NOT NULL
    and only `_to_utc` writes it; if it ever did, locking the club out of its
    own evening would be the worse failure.
    """
    try:
        day = parse_utc(row["starts_at"]).astimezone(tz).date()
    except (ValueError, TypeError):
        return True
    return day <= (now or datetime.now(tz)).astimezone(tz).date()


# -- who may look at a blind evening's names --------------------------------
#
# A blind evening keeps its names in the database and declines to print them.
# The one person that leaves stuck is the responsible: they registered the
# bottles and cannot see what they registered. So they, and only they, can ask
# for them back — for their own browser, never for anyone else's.
#
# What this is not is a lock. Without Cloudflare Access in front, signing in is
# the club password and then picking your own name, so "you are the
# responsible" stops an accidental tap and not a determined peek. What it does
# guarantee is that the names never reach a browser that has not asked: the
# page is rendered without them rather than hiding them with a stylesheet.

REVEALED = "revealed"          # session key: the event ids this browser asked for


def _blind(row) -> bool:
    """Whether this evening is one whose names are withheld at all."""
    return row["kind"] == "blind" and not row["tasting_id"]


def _may_reveal(row, member) -> bool:
    """Whether this person is the one who registered these bottles.

    `events.host` is filled from a dropdown of member names, so the two are the
    same string; folding them is insurance against a name being re-entered by
    hand one day, and costs nothing.
    """
    if not member or not row["host"]:
        return False
    return fold(member[1]) == fold(row["host"])


def _is_revealed(request: Request, row) -> bool:
    """Whether this browser has asked to see the names, and may.

    The permission is re-checked on every render rather than trusted from the
    session, so a reveal does not outlive being the responsible — if the
    evening is handed to somebody else, the old responsible's page goes blind
    again on its next load.
    """
    if not _blind(row):
        return False
    if row["id"] not in request.session.get(REVEALED, []):
        return False
    return _may_reveal(row, current_member(request))


# `Wine 3`, `wine3`, `Vin 3` — a bottle still carrying the name it was given
# before anybody knew what it was. Nothing refuses them: the club's own archive
# holds `Vin 1` from a survey that went in that way. They are only counted, so
# that closing a BYO evening says what is about to be filed.
PLACEHOLDER = re.compile(r"^\s*(wine|vin)\s*\d+\s*$", re.IGNORECASE)


def _unnamed(wines) -> int:
    """How many bottles are still called Wine N."""
    return sum(bool(PLACEHOLDER.match(w["name"] or "")) for w in wines)


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
    editing: int | None = None,
    status: int = 200,
):
    """The event's own page. Shared, so a refused bottle comes back on it."""
    wines = queries.event_wines(db, row["id"])
    member = current_member(request)
    # Worked out here rather than in Jinja, because the same answer decides
    # what the page prints *and* what two routes allow. Read from the request
    # rather than taken as an argument: a dozen routes reach this helper, and
    # `deps.get_member` is a thin wrapper over exactly this call.
    revealed = _is_revealed(request, row)
    return page(
        request,
        "events/detail.html",
        event=row,
        wines=wines,
        # Everything about a blind evening's page hangs off this one flag: the
        # names, the region and grape, the label pictures, what the Remove
        # button asks you to confirm, and whether Edit is offered at all.
        hidden=_blind(row) and not revealed,
        revealed=revealed,
        may_reveal=_may_reveal(row, member),
        # Said before closing, not after: a bottle filed as "Wine 3" joins a
        # cellar of named ones and nothing there can tell you what it was.
        unnamed=_unnamed(wines),
        now=utcnow_iso(),
        kinds=KINDS,
        pours=row["kind"] in POURING,
        # Computed here rather than reassembled in Jinja, so the button and the
        # route that refuses the same press cannot drift apart.
        too_early=not _day_has_come(row, cfg.tz),
        error=error,
        # What a photographed label read as, waiting to be checked. Empty the
        # rest of the time, which is what an untouched form is.
        prefill=prefill or {},
        # Which bottle's edit panel to open, and whose fields `prefill` belongs
        # to. None means the panel that adds a new one.
        editing=editing,
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


async def _read_label(photo: UploadFile | None, cfg) -> dict:
    """The photograph as a dict of fields, or raise LabelUnreadable saying why.

    Shared by the two label routes — adding a bottle and correcting one — so a
    photograph is checked, sized and read exactly once in this codebase.
    """
    if not cfg.has_label_reading:
        raise HTTPException(404, "No label reader configured")
    if photo is None or not photo.filename:
        raise label_reader.LabelUnreadable("No photo came through.")
    if (photo.size or 0) > label_reader.MAX_BYTES:
        raise label_reader.LabelUnreadable("That photo is too big — try again, or type it in.")
    if photo.content_type not in label_reader.ALLOWED_TYPES:
        raise label_reader.LabelUnreadable("That isn't a photo. Pick an image, or type it in.")
    try:
        image = await photo.read()
        found = await run_in_threadpool(
            label_reader.read_label, image, photo.content_type or "", cfg.anthropic_key
        )
    finally:
        await photo.close()
    return {k: v for k, v in found.model_dump().items() if v is not None}


@router.post("/{event_id}/wines/{wine_id}/label")
async def read_label_for_bottle(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    wine_id: int,
    photo: Annotated[UploadFile | None, File()] = None,
):
    """Photograph a bottle already on the list and fill its own form in.

    What a BYO evening is for: the wines go in as Wine 1, Wine 2, nobody knows
    whose is whose, and they are revealed one at a time as the evening goes on.
    At the reveal somebody points a phone at the bottle and this puts what the
    label says into that row's form, for them to check and press Save.

    Writes nothing, like its twin above — the model reading a decorative label
    can be confidently wrong, and a name in the cellar outlives the evening.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if _blind(row) and not _is_revealed(request, row):
        return _detail(request, db, cfg, row, status=400,
                       error="A blind evening's bottles are named when it closes.")
    try:
        found = await _read_label(photo, cfg)
    except label_reader.LabelUnreadable as exc:
        return _detail(request, db, cfg, row, error=str(exc), editing=wine_id, status=400)
    return _detail(request, db, cfg, row, prefill=found, editing=wine_id)


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
    try:
        found = await _read_label(photo, cfg)
    except label_reader.LabelUnreadable as exc:
        return _detail(request, db, cfg, row, error=str(exc), status=400)
    return _detail(request, db, cfg, row, prefill=found)


def _list_is_settled(row) -> str | None:
    """Why the running order cannot be changed, or None when it can.

    Once the evening has started, moving a bottle renumbers Wine 1…N under
    people who are halfway through a card — and on a blind evening they would
    be scoring a different bottle than the one in front of them without any
    sign of it. The votes themselves stay right, since they key on the row
    rather than the position, which is exactly what makes it silent.
    """
    if row["tasting_id"]:
        return "This evening is closed."
    if row["started_at"]:
        return "Voting has started — the running order is fixed now."
    return None


@router.post("/{event_id}/wines/order")
async def reorder(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    order: Annotated[str, Form()] = "",
):
    """The whole running order at once, as the drag posts it: "3,1,2"."""
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    settled = _list_is_settled(row)
    if settled:
        return _detail(request, db, cfg, row, error=settled, status=409)
    wanted = [int(part) for part in order.split(",") if part.strip().isdigit()]
    db.reorder_event_wines(event_id, wanted)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


# Registered above `/{wine_id}` on purpose: FastAPI matches in the order the
# routes are declared, and "order" would otherwise be read as a wine id —
# which fails as a 422 rather than as anything a reader would recognise.
@router.post("/{event_id}/wines/{wine_id}")
async def edit_wine(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    wine_id: int,
    name: Annotated[str, Form()] = "",
    producer: Annotated[str, Form()] = "",
    vintage: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    grape: Annotated[str, Form()] = "",
    price_nok: Annotated[str, Form()] = "",
    brought_by: Annotated[str, Form()] = "",
):
    """Correct a bottle already on the list.

    A blind evening's names are not shown until it is closed, and a form
    prefilled with one would undo that for anybody who opened it — so the page
    does not offer this, and nor does the route.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if _blind(row) and not _is_revealed(request, row):
        return _detail(request, db, cfg, row, status=400,
                       error="A blind evening's bottles are named when it closes.")
    if not name.strip():
        return _detail(request, db, cfg, row, error="A bottle needs a name.", status=400)
    db.update_event_wine(
        event_id, wine_id,
        name=name.strip(), producer=producer.strip(), vintage=_number(vintage),
        country=country.strip(), region=region.strip(), grape=grape.strip(),
        price_nok=_number(price_nok), brought_by=brought_by.strip(),
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/wines/{wine_id}/move")
async def move_wine(
    request: Request,
    db: Db,
    cfg: Cfg,
    _: LoggedIn,
    event_id: int,
    wine_id: int,
    direction: Annotated[str, Form()] = "up",
):
    """One step, from the ▲▼ buttons — the way that needs no JavaScript."""
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    settled = _list_is_settled(row)
    if settled:
        return _detail(request, db, cfg, row, error=settled, status=409)
    db.move_event_wine(event_id, wine_id, up=direction != "down")
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/wines/{wine_id}/delete")
async def remove_wine(request: Request, db: Db, _: LoggedIn, event_id: int, wine_id: int):
    db.delete_event_wine(event_id, wine_id)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/reopen")
async def reopen(request: Request, db: Db, cfg: Cfg, _: LoggedIn, event_id: int):
    """Take a closed evening back out of the archive.

    Destructive in one direction only: it removes what closing *copied* into
    the cellar. The bottles and the cards never left the staging tables, so the
    evening comes back whole.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    refused = db.reopen_event(event_id)
    if refused:
        return _detail(request, db, cfg, row, error=refused, status=409)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/reveal")
async def reveal(request: Request, db: Db, cfg: Cfg, _: LoggedIn, event_id: int):
    """Show the responsible the bottles they registered, or hide them again.

    A toggle, because the button's own label says which way it will go. What it
    writes is one number in this browser's session — nothing in the database,
    because a reveal is the responsible's own business and not something that
    happens to the other eight.

    The rule is here and not only in the markup. A page that merely omits a
    button is a page whose route can still be posted to.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if not _blind(row):
        return _detail(request, db, cfg, row, status=400,
                       error="Nothing is hidden on this evening.")
    if not _may_reveal(row, current_member(request)):
        return _detail(request, db, cfg, row, status=403,
                       error="Only whoever is responsible for the evening can "
                             "see the bottles before it is over.")

    # A list rather than a set: the session is stored as JSON.
    asked = [i for i in request.session.get(REVEALED, []) if i != event_id]
    if not _is_revealed(request, row):
        asked.append(event_id)
    request.session[REVEALED] = asked
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/{event_id}/close")
async def close(request: Request, db: Db, _: LoggedIn, event_id: int):
    """The end of an evening: its bottles and its scores join the archive.

    Called close rather than promote because that is what the club presses —
    `db.promote_event` keeps the older name, since filing an evening in the
    archive is what this does to the *history*, and closing is what it does to
    the *event*. Pressing it twice is harmless; the second does nothing.
    """
    if queries.event(db, event_id) is None:
        raise HTTPException(404, "No such event")
    db.promote_event(event_id)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


# -- the voting page --------------------------------------------------------
#
# The first thing this app does *during* an evening rather than after it. The
# bottles are already staged in `event_wines`; the scores stage beside them in
# `event_votes`, and neither reaches the cellar until somebody closes the
# evening. That is what lets a card be changed all night and still leave one
# clean row per member in fifteen years of history.


def _open_for_voting(row) -> str | None:
    """Why this evening cannot be voted on, or None when it can be.

    One place, because there are four ways to get it wrong and each of them
    should say what is actually true rather than 404 at a puzzled member.
    """
    if row["kind"] not in POURING:
        return "Only a tasting is scored."
    if not row["started_at"]:
        return "This evening has not started yet."
    if row["tasting_id"]:
        return "This evening is closed — its scores are in the cellar now."
    return None


def _vote_page(request: Request, db: Db, row, member, *,
               error: str | None = None, saved: bool = False,
               card: dict[int, int] | None = None, status: int = 200):
    wines = queries.event_wines(db, row["id"])
    return page(
        request,
        "events/vote.html",
        event=row,
        wines=wines,
        kinds=KINDS,
        # A blind evening's bottles stay Wine 1 … Wine N until it is closed,
        # which is the entire point of the names living in the database while
        # the page declines to print them.
        hidden=row["kind"] == "blind" and not row["tasting_id"],
        scale=SCALE,
        usual=USUAL,
        card=card if card is not None else (
            queries.my_votes(db, row["id"], member[0]) if member else {}
        ),
        voted=queries.who_has_voted(db, row["id"]),
        error=error,
        saved=saved,
        status_code=status,
    )


@router.get("/{event_id}/vote")
async def vote(request: Request, db: Db, cfg: Cfg, member: Member, _: LoggedIn,
               event_id: int):
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    closed = _open_for_voting(row)
    if closed:
        return _detail(request, db, cfg, row, error=closed, status=409)
    # Set by the redirect after a submit, so a reload cannot re-post the card
    # and the page can still say the scores landed.
    return _vote_page(request, db, row, member,
                      saved=request.query_params.get("saved") == "1")


@router.post("/{event_id}/start")
async def start(request: Request, db: Db, cfg: Cfg, _: LoggedIn, event_id: int):
    """Open the voting, and take whoever pressed it straight to the page."""
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    if row["kind"] not in POURING:
        return _detail(request, db, cfg, row, error="Only a tasting is scored.", status=400)
    # The greyed button is a hint to a person; this is the rule. Anyone can
    # post the form, and starting an evening cannot be undone.
    if not _day_has_come(row, cfg.tz):
        return _detail(
            request, db, cfg, row, status=400,
            error=f"Too early — this evening can be started on "
                  f"{fmt_day(row['starts_at'], str(cfg.tz))}.",
        )
    if not queries.event_wines(db, event_id):
        return _detail(request, db, cfg, row,
                       error="Line up at least one bottle first.", status=400)
    db.start_event(event_id)
    return RedirectResponse(f"/events/{event_id}/vote", status_code=303)


@router.post("/{event_id}/vote")
async def cast(request: Request, db: Db, cfg: Cfg, member: Member, _: LoggedIn,
               event_id: int):
    """One member's whole card at once, replacing whatever they said before.

    The form posts `score-<event_wine_id>` per bottle, and the typed box wins
    over the dropdown when they disagree — the page keeps them in step, but the
    person who typed a number meant it.
    """
    row = queries.event(db, event_id)
    if row is None:
        raise HTTPException(404, "No such event")
    closed = _open_for_voting(row)
    if closed:
        return _detail(request, db, cfg, row, error=closed, status=409)
    if member is None:
        # A vote has to belong to somebody; the page says so and links to the
        # prompt rather than filing it under nobody.
        return _vote_page(request, db, row, member,
                          error="Tell us who you are first.", status=400)

    form = await request.form()
    card: dict[int, int] = {}
    for wine in queries.event_wines(db, event_id):
        typed = str(form.get(f"typed-{wine['id']}") or "").strip()
        picked = str(form.get(f"score-{wine['id']}") or "").strip()
        given = typed or picked
        if not given:
            continue                      # a bottle nobody got to stays blank
        try:
            score = int(given)
        except ValueError:
            return _vote_page(request, db, row, member, card=card, status=400,
                              error=f"{given!r} is not a score.")
        if not LOWEST <= score <= HIGHEST:
            return _vote_page(request, db, row, member, card=card, status=400,
                              error=f"{score} is not on the scale — it runs 1 to 100.")
        card[wine["id"]] = score

    db.record_votes(event_id, member[0], card)
    return RedirectResponse(f"/events/{event_id}/vote?saved=1", status_code=303)
