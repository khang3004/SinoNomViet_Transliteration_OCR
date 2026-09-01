"""The reviewer's endpoints: get work, look at it, record a verdict.

Reviewers hold disjoint sets of records, so every route here is scoped to the
caller's own username — except the team view, which is deliberately open to
everyone so reviewers can see each other's work.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.auth import current_user
from app.core.audit import AuditError, QueueItem
from app.core.models import GeminiVerdict, Verdict

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _runtime(request: Request):
    return request.app.state.runtime


def _present(runtime, item: QueueItem) -> dict:
    url = runtime.image_url(item.record.post_id, item.record.idx, item.record.suffix)
    return item.to_json(image_url=url)


# --- work list ---------------------------------------------------------

@router.get("/queue")
async def my_queue(
    request: Request,
    include_done: bool = Query(True),
    user: dict = Depends(current_user),
):
    """Everything assigned to the caller, unreviewed first."""
    runtime = _runtime(request)
    items = runtime.audit.queue(user["username"], include_done=include_done)
    done = sum(1 for i in items if i.done)
    return {
        "username": user["username"],
        "total": len(items),
        "done": done,
        "remaining": len(items) - done,
        "items": [_present(runtime, item) for item in items],
    }


class AssignRequest(BaseModel):
    count: int = Field(default=0, ge=0, le=5000)


@router.post("/queue/assign")
async def assign_more(
    request: Request, body: AssignRequest, user: dict = Depends(current_user)
):
    """Draw another stratified batch for the caller.

    This is also the top-up path: a reviewer who flags images as unusable is
    short of their target and asks for replacements here.
    """
    runtime = _runtime(request)
    cfg = runtime.settings.sampling
    count = body.count or cfg.default_batch
    if count > cfg.max_batch:
        raise HTTPException(400, f"at most {cfg.max_batch} images per request")

    try:
        result = runtime.audit.assign(
            user["username"],
            count,
            targets=cfg.targets,
            per_post_cap=cfg.per_post_cap,
        )
    except AuditError as exc:
        raise HTTPException(400, str(exc)) from exc

    payload = result.to_json()
    if result.short:
        payload["message"] = (
            f"Only {len(result.records)} of {count} could be drawn — the pool is "
            "running out, or the per-post cap is holding the rest back."
        )
    return payload


# --- verdicts ----------------------------------------------------------

class ReviewRequest(BaseModel):
    record_id: str
    verdict: str
    corrected: str = ""
    note: str = ""
    gemini_verdict: str = "skipped"
    seconds_spent: float = 0.0


@router.post("/review")
async def submit_review(
    request: Request, body: ReviewRequest, user: dict = Depends(current_user)
):
    runtime = _runtime(request)
    try:
        verdict = Verdict(body.verdict)
    except ValueError:
        raise HTTPException(
            400, f"verdict must be one of: {', '.join(v.value for v in Verdict)}"
        ) from None
    try:
        gemini_verdict = GeminiVerdict(body.gemini_verdict or "skipped")
    except ValueError:
        gemini_verdict = GeminiVerdict.SKIPPED

    try:
        review = runtime.audit.submit(
            user["username"],
            body.record_id,
            verdict,
            corrected=body.corrected,
            note=body.note,
            gemini_verdict=gemini_verdict,
            seconds_spent=body.seconds_spent,
            is_admin=user.get("role") == "admin",
        )
    except AuditError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "review": review.to_json(),
        # A flagged image is handed back, so the caller is now owed a
        # replacement — the UI uses this to offer a top-up.
        "released": verdict is Verdict.NOT_AN_IMAGE,
    }


class ReleaseRequest(BaseModel):
    record_id: str


@router.post("/queue/release")
async def release(
    request: Request, body: ReleaseRequest, user: dict = Depends(current_user)
):
    """Hand a record back without judging it."""
    runtime = _runtime(request)
    holder = runtime.audit.active_assignments().get(body.record_id)
    if holder is None:
        raise HTTPException(404, "not assigned")
    if holder.username != user["username"] and user.get("role") != "admin":
        raise HTTPException(403, f"assigned to {holder.username}")
    runtime.audit.release(body.record_id, user["username"])
    return {"ok": True, "record_id": body.record_id}


# --- shared views ------------------------------------------------------

@router.get("/reviewed")
async def reviewed(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
    reviewer: str = Query(""),
    q: str = Query(""),
    user: dict = Depends(current_user),
):
    """Everyone's completed reviews — reviewers can see each other's work."""
    runtime = _runtime(request)
    items = runtime.audit.all_reviewed([reviewer] if reviewer else None)

    needle = q.strip().lower()
    if needle:
        items = [i for i in items if _matches(i, needle)]

    window = items[offset : offset + limit]
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(window) < len(items),
        "items": [_present(runtime, item) for item in window],
    }


def _matches(item: QueueItem, needle: str) -> bool:
    record, review = item.record, item.review
    haystack = " ".join(
        [
            record.post_id, record.image, record.caption,
            record.ground_truth, record.gemini, record.post_link,
            review.corrected if review else "",
            review.note if review else "",
            review.username if review else "",
        ]
    ).lower()
    return needle in haystack


@router.get("/progress")
async def progress(request: Request, user: dict = Depends(current_user)):
    """The panel: distribution fill, verdict mix, and the headline accuracies."""
    runtime = _runtime(request)
    return {
        **runtime.audit.progress(targets=runtime.settings.sampling.targets),
        "reviewers": runtime.audit.per_reviewer(),
        "status": runtime.status(),
    }
