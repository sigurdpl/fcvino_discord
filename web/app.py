"""The FC Vino web app.

    uvicorn web.app:app --reload

Runs beside the Discord bot, on the same SQLite file. That is safe because
`bot/db.py` opens the database in WAL mode, which allows readers from another
process while one writer works — the two halves of the club can be up at once,
and the search index notices when the bot has changed something underneath it.

Nothing here owns the schema. `bot/db.py` does.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from bot import config
from bot.db import Database
from bot.football_api import FootballAPI

from .deps import WEB_ROOT, LoginRequired, redirect_to_login
from .queries import IndexCache
from .routes import auth as auth_routes
from .routes import football, home, trips, wine

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    db = Database(cfg.db_path)
    db.connect()
    app.state.db = db
    app.state.index = IndexCache()
    app.state.football = FootballAPI(cfg.football_token) if cfg.has_football else None
    log.info("web app ready on %s", cfg.db_path)
    try:
        yield
    finally:
        if app.state.football is not None:
            await app.state.football.close()
        db.close()


def create_app(cfg: config.Config | None = None) -> FastAPI:
    cfg = cfg or config.load(require_discord=False, require_web=True)

    app = FastAPI(title="FC Vino", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.web_secret,
        session_cookie="fcvino",
        # Secure once it is hosted behind Cloudflare, plain http on localhost.
        https_only=cfg.access_trusted,
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return redirect_to_login(request, exc.next_url)

    app.include_router(auth_routes.router)
    app.include_router(home.router)
    app.include_router(wine.router)
    app.include_router(trips.router)
    app.include_router(football.router)

    return app


def __getattr__(name: str):
    """Build the app only when something actually asks for it.

    `uvicorn web.app:app` reaches this and gets a configured app. Importing
    `create_app` from a test does not, so the tests never need a WEB_PASSWORD
    in the environment to import this module.
    """
    if name == "app":
        return create_app()
    raise AttributeError(name)
