"""Admin gallery: browse prepared images and the posts they came from."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gallery", dependencies=[Depends(current_user)])


@router.get("")
async def browse(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=120),
    q: str = Query("", max_length=200),
):
    """A page of prepared posts, newest first.

    ``q`` filters on post id, author, caption and post link — enough to find
    "that one post" without needing a real index.
    """
    gallery = request.app.state.runtime.gallery
    import asyncio

    page = await asyncio.to_thread(gallery.page, offset, limit, q)
    return page.to_json()


@router.get("/stats")
async def stats(request: Request):
    import asyncio

    gallery = request.app.state.runtime.gallery
    return await asyncio.to_thread(gallery.stats)


@router.get("/{post_id}")
async def get_post(request: Request, post_id: str):
    """Everything known about one post — backs the lightbox."""
    import asyncio

    gallery = request.app.state.runtime.gallery
    record = await asyncio.to_thread(gallery.get, post_id)
    if record is None:
        raise HTTPException(404, "no prepared record for that post")
    return record
