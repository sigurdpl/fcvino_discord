"""Signing in and picking your name."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import auth, queries
from ..deps import Cfg, Db, page, safe_path

router = APIRouter()


@router.get("/login")
async def login_form(request: Request, db: Db):
    if auth.is_signed_in(request):
        return RedirectResponse("/wine", status_code=303)
    return page(request, "login.html", members=queries.members(db), error=None)


@router.post("/login")
async def login(request: Request, db: Db, cfg: Cfg, password: Annotated[str, Form()] = ""):
    if not auth.password_ok(password, cfg.web_password):
        return page(
            request,
            "login.html",
            members=queries.members(db),
            error="That isn't the club password.",
            status_code=401,
        )
    auth.sign_in(request)
    # Back to whatever they were reaching for, but only ever to a path on this
    # site — an open redirect is a silly thing to hand-roll.
    target = safe_path(request.session.pop(auth.NEXT_URL, None), request.url.netloc)
    return RedirectResponse(target, status_code=303)


@router.post("/whoami")
async def whoami(request: Request, db: Db, member_id: Annotated[int, Form()]):
    """Claim a name from the club's records.

    Only a name that already exists is accepted, so a hand-edited form cannot
    invent a tenth member.
    """
    if not auth.is_signed_in(request):
        return RedirectResponse("/login", status_code=303)
    match = next((m for m in queries.members(db) if m["id"] == member_id), None)
    if match is not None:
        auth.claim_member(request, match["id"], match["name"])
    back = safe_path(request.headers.get("referer"), request.url.netloc)
    return RedirectResponse(back, status_code=303)


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request):
    auth.sign_out(request)
    return RedirectResponse("/login", status_code=303)
