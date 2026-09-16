"""Who Cloudflare Access says you are.

The site sits behind a Cloudflare tunnel with a Zero Trust Access application in
front of it: you prove an email address with a one-time PIN, and an Access policy
decides whether that address is one of ours. By the time a request reaches this
app, that has already happened — so asking again for a shared club password adds
a step without adding a check.

Cloudflare sets `Cf-Access-Authenticated-User-Email` on requests that pass a
policy, and strips any copy a client tries to send of its own. Trusting it is
safe **only because uvicorn binds 127.0.0.1 and the tunnel is the one route to
the origin** — nothing else can reach this app to forge the header. That is why
the trust is opt-in via `FCVINO_ACCESS` rather than simply believing the header
whenever it appears: a local run, or a run bound to 0.0.0.0, must not inherit it.

If the app is ever exposed directly, this header check has to be replaced by
verifying the `Cf-Access-Jwt-Assertion` JWT against the team's signing keys at
`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, with the
application's AUD as the expected audience. That needs a JWT library; the header
does not, which is the whole reason it is enough here.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.requests import Request

from bot.config import Config
from bot.wine_origin import fold

EMAIL_HEADER = "cf-access-authenticated-user-email"

# Cloudflare's own logout endpoint: it clears the Access cookie at the edge.
# Without it, signing out of this app just hands you straight back in.
LOGOUT_URL = "/cdn-cgi/access/logout"


def identity(request: Request, cfg: Config) -> str | None:
    """The email Cloudflare vouched for, lowercased, or None."""
    if not cfg.access_trusted:
        return None
    email = (request.headers.get(EMAIL_HEADER) or "").strip().lower()
    return email or None


def member_name(email: str, cfg: Config, known: Sequence[str]) -> str | None:
    """Which club member this address belongs to, or None to let them pick.

    Cloudflare knows an email address and nothing else — with a one-time PIN
    there is no name behind it — while every score and note here is filed under
    a member's name. Most of the club's addresses start with their own first
    name, so that is read off the address and only the ones that don't need
    writing down in `FCVINO_ACCESS_MEMBERS`.

    The map is consulted first so it can overrule a wrong guess: a name claimed
    this way sticks in the session, and signing out would only derive the same
    one again, leaving `.env` as the one place to correct it.

    Matching is on folded text but always whole — the local part, or its first
    token for `firstname.lastname@` addresses. Never a prefix: `tor@` must not
    walk in as Tore.
    """
    address = email.strip().lower()
    mapped = cfg.access_members.get(address)
    if mapped:
        return mapped

    # fold() is the search box's normaliser, so `havard@` finds Håvard for the
    # same reason `sor afrika` finds Sør Afrika.
    local = fold(address.partition("@")[0]).strip()
    if not local:
        return None
    wanted = {local, local.partition(" ")[0]}
    return next((name for name in known if fold(name).strip() in wanted), None)
