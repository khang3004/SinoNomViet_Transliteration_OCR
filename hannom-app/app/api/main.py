"""FastAPI application — the HTTP adapter around ``app.core``.

Run:  uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers 1

``--workers 1`` is required, not a suggestion: assignment is serialised by an
in-process lock, and a second worker would hand the same image to two reviewers.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api import routes_corpus, routes_images, routes_review, routes_users
from app.api.auth import (
    COOKIE_NAME,
    AuthConfig,
    AuthNotConfigured,
    LoginThrottle,
    current_user,
    is_public_path,
    make_token,
    user_from_request,
)
from app.api.runtime import Runtime
from app.core.config import describe_secrets, load_settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("hannom")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    await app.state.runtime.startup()

    # Log only whether each secret exists, never its value.
    log.info("secrets present: %s", describe_secrets())
    log.info(
        "sampling: batch=%d per_post_cap=%d targets=%s",
        settings.sampling.default_batch,
        settings.sampling.per_post_cap,
        {b.value: round(w, 3) for b, w in settings.sampling.targets.items()},
    )
    if not settings.drive.configured:
        log.warning(
            "GOOGLE_DRIVE_FOLDER_ID is not set — images cannot be fetched."
        )
    elif settings.drive.complete_listing:
        log.info("drive folder: %s (service account)", settings.drive.folder_id)
    else:
        log.warning(
            "drive folder: %s (public listing, capped at 5500 files). Set "
            "GOOGLE_SERVICE_ACCOUNT to index a larger folder.",
            settings.drive.folder_id,
        )
    if not settings.images.signing_secret:
        log.warning(
            "IMAGE_SIGNING_SECRET is not set — image URLs cannot be minted and "
            "no thumbnails will load."
        )
    if not settings.images.public_base_url:
        log.warning("PUBLIC_BASE_URL is not set — image URLs will be relative")

    try:
        yield
    finally:
        await app.state.runtime.shutdown()


def create_app() -> FastAPI:
    settings = load_settings()
    auth = AuthConfig.from_env()
    # Refuse to start unprotected: this app is internet-facing on a domain.
    auth.validate()

    app = FastAPI(title="OCR Review", version="4.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth = auth
    app.state.throttle = LoginThrottle()
    app.state.runtime = Runtime(settings)
    app.state.users = app.state.runtime.users

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        if is_public_path(request.url.path):
            return await call_next(request)
        user = user_from_request(request, auth.secret)
        if user is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
            return HTMLResponse(_login_page(), status_code=401)
        request.state.user = user
        return await call_next(request)

    app.include_router(routes_images.router)
    app.include_router(routes_review.router)
    app.include_router(routes_corpus.router)
    app.include_router(routes_users.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    _register_core_routes(app, auth)
    return app


class LoginRequest(BaseModel):
    username: str
    password: str


def _register_core_routes(app: FastAPI, auth: AuthConfig) -> None:
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "ts": time.time()}

    @app.post("/api/auth/login")
    async def login(request: Request, body: LoginRequest, response: Response):
        throttle: LoginThrottle = app.state.throttle
        ip = request.client.host if request.client else "unknown"

        remaining = throttle.locked_out(ip)
        if remaining > 0:
            raise HTTPException(429, f"too many attempts; retry in {int(remaining)}s")

        user = auth.authenticate(body.username, body.password, app.state.users)
        if user is None:
            throttle.record_failure(ip)
            log.warning("failed login for %r from %s", body.username, ip)
            raise HTTPException(401, "invalid credentials")

        throttle.reset(ip)
        app.state.users.record_login(user.username)
        response.set_cookie(
            COOKIE_NAME,
            make_token(user.username, user.role, auth.secret),
            httponly=True,
            secure=auth.cookie_secure,
            samesite="strict",  # single-origin app, so Strict is free CSRF cover
            max_age=7 * 24 * 3600,
            path="/",
        )
        return {"username": user.username, "role": user.role}

    @app.post("/api/auth/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        session = user_from_request(request, auth.secret)
        if session is None:
            raise HTTPException(401, "not authenticated")
        # Re-read the store so a role change or a disabled account is reflected
        # without waiting for the seven-day token to expire.
        stored = app.state.users.get(session["username"])
        if stored is None:
            return session
        if not stored.active:
            raise HTTPException(403, "account disabled")
        return {"username": stored.username, "role": stored.role,
                "display_name": stored.display_name}

    @app.get("/", response_class=HTMLResponse)
    async def index(user: dict = Depends(current_user)):
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>OCR Review</h1><p>UI not found.</p>", 500)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))


def _login_page() -> str:
    login_file = STATIC_DIR / "login.html"
    if login_file.exists():
        return login_file.read_text(encoding="utf-8")
    return "<h1>OCR Review</h1><p>Login required.</p>"


try:
    app = create_app()
except AuthNotConfigured as exc:
    # Surface the reason clearly instead of a bare import traceback.
    log.error("%s", exc)
    raise
