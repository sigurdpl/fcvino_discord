"""Signing in: one club password, then say which member you are.

Deliberately modest, because the app currently runs on one laptop at
`localhost:8000`. Two things it still gets right: the password is compared in
constant time, and the session cookie is signed — so "I am Tore" cannot be
forged by editing a cookie, only by knowing the password in the first place.

The honest limitation, worth stating plainly rather than burying: **anyone with
the password can pick any name.** Identity is on the honour system among nine
friends. When this moves off localhost, `wine_members.discord_id` is already
there for Discord OAuth to bind a login to a real person.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request

AUTHED = "authed"
MEMBER_ID = "member_id"
MEMBER_NAME = "member_name"
NEXT_URL = "next_url"


def password_ok(supplied: str, expected: str | None) -> bool:
    """Constant-time comparison, false when no password is configured.

    A blank `WEB_PASSWORD` must never mean "everyone is welcome", so an unset
    password rejects every attempt rather than accepting them all.
    """
    if not expected:
        return False
    return secrets.compare_digest(supplied.strip(), expected.strip())


def sign_in(request: Request) -> None:
    request.session[AUTHED] = True


def sign_out(request: Request) -> None:
    request.session.clear()


def is_signed_in(request: Request) -> bool:
    return bool(request.session.get(AUTHED))


def claim_member(request: Request, member_id: int, name: str) -> None:
    request.session[MEMBER_ID] = int(member_id)
    request.session[MEMBER_NAME] = name


def current_member(request: Request) -> tuple[int, str] | None:
    """Who this session says it is, or None if they have not picked yet."""
    member_id = request.session.get(MEMBER_ID)
    name = request.session.get(MEMBER_NAME)
    if member_id is None or not name:
        return None
    return int(member_id), name
