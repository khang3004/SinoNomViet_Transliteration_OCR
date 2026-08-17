"""Public image serving for the Gemini stage.

The downstream flow is::

    JSONL (our URLs) -> Gemini Batch -> Gemini fetches the image -> OCR

Gemini fetches anonymously, so these URLs cannot sit behind the session cookie.
They are guarded by an HMAC signature instead: ``/img/*`` needs no login, but
only URLs this service minted will serve. Path-safety rules live in
``app.core.imagestore``; signing lives in ``app.core.signing``.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.core.config import Settings
from app.core.imagestore import (
    content_type_for,
    is_safe_post_id,
    parse_image_filename,
    resolve_image_path,
)
from app.core.signing import verify

log = logging.getLogger(__name__)

router = APIRouter()

# Per-IP rate limit: this endpoint is unauthenticated and internet-facing.
_RATE_WINDOW_S = 60.0
_RATE_MAX = 600
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


@router.get("/img/{post_id}/{filename}")
async def serve_image(
    request: Request,
    post_id: str,
    filename: str,
    exp: int = Query(..., description="Signature expiry (unix seconds)"),
    sig: str = Query(..., description="HMAC-SHA256 signature"),
):
    settings: Settings = request.app.state.settings

    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        raise HTTPException(429, "rate limit exceeded")

    if not is_safe_post_id(post_id):
        raise HTTPException(400, "invalid post id")

    parsed = parse_image_filename(filename)
    if parsed is None:
        raise HTTPException(400, "invalid image name")
    idx, suffix = parsed

    # Signature first: never touch the filesystem for an unsigned request.
    if not verify(post_id, idx, exp, sig, settings.images.signing_secret):
        raise HTTPException(403, "invalid or expired signature")

    path = resolve_image_path(settings.images_dir, post_id, idx, suffix)
    if path is None:
        raise HTTPException(404, "image not found")

    return FileResponse(
        path,
        media_type=content_type_for(path),
        headers={
            # Cacheable, but never by shared proxies — the URL carries a signature.
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
