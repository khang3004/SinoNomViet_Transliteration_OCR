"""FastAPI application — the HTTP adapter around ``app.core``.

Run:  uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers 1

``--workers 1`` is required, not a suggestion: batch state lives in this process,
and a second worker would fork it and corrupt progress tracking.
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

from app.api import routes_images, routes_jobs, routes_uploads
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
from app.core.ocr import decode_image, scan_array

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("hannom")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    for directory in (settings.data_dir, settings.images_dir, settings.jobs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    present = describe_secrets()
    # Log only whether each secret exists, never its value.
    log.info("secrets present: %s", {k: v for k, v in present.items()})
    log.info(
        "config: workers=%d batch=%d interval=%ss engine=%s",
        settings.ocr.workers, settings.batch_size,
        settings.scan_interval_s, settings.ocr.engine_name,
    )
    if settings.minio_enabled:
        log.info("input: upload or MinIO (%s)", settings.minio.endpoint)
    else:
        # Not a warning: upload/download is the supported default flow.
        log.info("input: upload only (MinIO not configured)")
    if not settings.images.public_base_url:
        log.warning("PUBLIC_BASE_URL is not set — minted image URLs will be relative")
    if not settings.images.signing_secret:
        # This one fails late — during publish, after downloads and OCR — so it
        # is worth shouting about at startup.
        log.warning(
            "IMAGE_SIGNING_SECRET is not set — batches will FAIL at the publish "
            "phase, after downloading and scanning. Set it before running."
        )

    await app.state.runtime.startup()
    try:
        yield
    finally:
        await app.state.runtime.shutdown()


def create_app() -> FastAPI:
    settings = load_settings()
    auth = AuthConfig.from_env()
    # Refuse to start unprotected: this app is internet-facing on a domain.
    auth.validate()

    app = FastAPI(title="Han Scanner", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth = auth
    app.state.throttle = LoginThrottle()
    app.state.runtime = Runtime(settings)

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
    app.include_router(routes_jobs.router)
    app.include_router(routes_uploads.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    _register_core_routes(app, auth)
    return app


class LoginRequest(BaseModel):
    username: str
    password: str


class ScanRequest(BaseModel):
    """Single-image demo scan — the original paste-JSON workflow."""

    images: list[str] = []
    image_urls: list[str] = []
    post_id: str = "demo"


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

        if not auth.authenticate(body.username, body.password):
            throttle.record_failure(ip)
            log.warning("failed login for %r from %s", body.username, ip)
            raise HTTPException(401, "invalid credentials")

        throttle.reset(ip)
        response.set_cookie(
            COOKIE_NAME,
            make_token(body.username, auth.secret),
            httponly=True,
            secure=auth.cookie_secure,
            samesite="strict",  # single-origin app, so Strict is free CSRF cover
            max_age=7 * 24 * 3600,
            path="/",
        )
        return {"username": body.username}

    @app.post("/api/auth/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        user = user_from_request(request, auth.secret)
        if user is None:
            raise HTTPException(401, "not authenticated")
        return user

    @app.get("/", response_class=HTMLResponse)
    async def index(user: dict = Depends(current_user)):
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Han Scanner</h1><p>UI not found.</p>", 500)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    @app.post("/api/scan")
    async def scan_one(body: ScanRequest, user: dict = Depends(current_user)):
        """Demo endpoint: scan a single image URL right now.

        Kept from the original app for spot-checking. The batch pipeline does not
        use this path — it scans from local files after the download phase.
        """
        import httpx

        urls = body.image_urls or body.images
        if not urls:
            raise HTTPException(400, "no image url provided")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(urls[0])
            if resp.status_code != 200:
                raise HTTPException(502, f"fetch failed: HTTP {resp.status_code}")
            data = resp.content

        settings = app.state.settings
        try:
            result = scan_array(
                decode_image(data),
                lang=settings.ocr.lang,
                min_confidence=settings.ocr.min_confidence,
                enable_mkldnn=settings.ocr.enable_mkldnn,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"scan failed: {exc}") from exc

        return {"post_id": body.post_id, "image_url": urls[0], **result}


def _login_page() -> str:
    login_file = STATIC_DIR / "login.html"
    if login_file.exists():
        return login_file.read_text(encoding="utf-8")
    return "<h1>Han Scanner</h1><p>Login required.</p>"


try:
    app = create_app()
except AuthNotConfigured as exc:
    # Surface the reason clearly instead of a bare import traceback.
    log.error("%s", exc)
    raise
