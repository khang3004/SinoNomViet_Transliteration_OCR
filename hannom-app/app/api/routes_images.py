"""Serving review images.

The images live in the upstream team's public Drive folder. Rather than pointing
reviewers' browsers at Drive directly — which would leak the folder to anyone
who opened devtools, and rate-limit under a room full of reviewers — this route
mirrors each image on first request and serves it from local disk thereafter.

``/img/*`` is signature-guarded rather than cookie-guarded so an ``<img>`` tag
loads without a session round-trip. Path-safety rules live in
``app.core.imagestore``; signing lives in ``app.core.signing``.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.core.config import Settings
from app.core.drive import DriveError
from app.core.imagestore import (
    content_type_for,
    is_safe_slug,
    parse_image_filename,
    resolve_image_path,
)
from app.core.postid import unslug
from app.core.signing import verify

log = logging.getLogger(__name__)

router = APIRouter()

# Per-IP rate limit: this endpoint is unauthenticated and internet-facing.
_RATE_WINDOW_S = 60.0
_RATE_MAX = 1200
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    bucket = _hits[ip]
    while bucket and now - bucket[0] > _RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= _RATE_MAX:
        return True
    bucket.append(now)
    return False


@router.get("/img/{post_slug}/{filename}")
async def serve_image(
    request: Request,
    post_slug: str,
    filename: str,
    exp: int = Query(..., description="Signature expiry (unix seconds)"),
    sig: str = Query(..., description="HMAC-SHA256 signature"),
):
    settings: Settings = request.app.state.settings
    runtime = request.app.state.runtime

    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        raise HTTPException(429, "rate limit exceeded")

    if not is_safe_slug(post_slug):
        raise HTTPException(400, "invalid post id")

    parsed = parse_image_filename(filename)
    if parsed is None:
        raise HTTPException(400, "invalid image name")
    idx, suffix = parsed

    # Signature first: never touch the filesystem or Drive for an unsigned request.
    if not verify(post_slug, idx, exp, sig, settings.images.signing_secret):
        raise HTTPException(403, "invalid or expired signature")

    path = resolve_image_path(settings.images_dir, post_slug, idx, suffix)
    if path is None:
        path = await _mirror(runtime, post_slug, idx, suffix)

    return FileResponse(
        path,
        media_type=content_type_for(path),
        headers={
            # Cacheable, but never by shared proxies — the URL carries a signature.
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _mirror(runtime, post_slug: str, idx: int, suffix: str):
    """Pull one image from Drive on first view.

    The record is looked up rather than trusting the URL, because the Drive
    index is keyed on the upstream filename — which only the record knows.
    """
    post_id = unslug(post_slug)
    record = runtime.audit.corpus.get(f"{post_slug}:{idx}")
    image_name = record.image if record is not None else ""
    if not image_name:
        raise HTTPException(404, "image not found")

    try:
        return await runtime.images.ensure(post_id, idx, image_name, suffix)
    except DriveError as exc:
        log.warning("drive fetch failed for %s: %s", image_name, exc)
        raise HTTPException(502, f"could not fetch image from Drive: {exc}") from exc
