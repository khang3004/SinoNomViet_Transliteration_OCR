"""Job control and monitoring endpoints. All require a session."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.auth import current_user
from app.core.health import image_store, snapshot
from app.core.jobstore import Phase

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


class CreateJobRequest(BaseModel):
    limit: int | None = Field(None, ge=1, le=20000)
    # Preflight pauses when too many URLs are already dead; this overrides it.
    confirm_expired: bool = False


@router.post("/jobs")
async def create_job(request: Request, body: CreateJobRequest) -> dict[str, Any]:
    runtime = request.app.state.runtime
    if runtime.busy:
        raise HTTPException(409, "a batch is already running")
    job_id = await runtime.start_batch(
        limit=body.limit, confirm_expired=body.confirm_expired
    )
    return {"job_id": job_id, "status": "started"}


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = Query(25, ge=1, le=200)):
    store = request.app.state.runtime.jobstore
    return {"jobs": [s.to_json() for s in store.list_states(limit)]}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    runtime = request.app.state.runtime
    job_dir = runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")
    state = job_dir.load_state()
    if state is None:
        raise HTTPException(404, "job state unreadable")

    payload = state.to_json()
    payload["event_count"] = job_dir.event_count()
    payload["is_active"] = not state.phase.terminal
    return payload


@router.get("/jobs/{job_id}/events")
async def get_events(
    request: Request,
    job_id: str,
    cursor: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    """Incremental log tail.

    Cursor-based rather than streamed: a browser that reconnects, or one opened
    hours later, gets exactly the lines it has not seen.
    """
    job_dir = request.app.state.runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")
    events, next_cursor = job_dir.read_events(cursor, limit)
    return {"events": events, "cursor": next_cursor}


@router.get("/jobs/{job_id}/results")
async def get_results(
    request: Request,
    job_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    job_dir = request.app.state.runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")
    rows = job_dir.read_results()
    return {"total": len(rows), "results": rows[offset : offset + limit]}


@router.get("/jobs/{job_id}/errors")
async def get_job_errors(request: Request, job_id: str):
    """Download + OCR failures for this batch, grouped by class.

    Grouping is what makes 400 failures actionable: "all http_403" means the
    crawl went stale, "all decode_error" means something else entirely.
    """
    job_dir = request.app.state.runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")

    grouped: dict[str, dict[str, Any]] = {}
    for row in job_dir.read_downloads() + job_dir.read_results():
        if row.get("ok"):
            continue
        cls = row.get("error_class") or "unknown"
        bucket = grouped.setdefault(cls, {"count": 0, "sample": None})
        bucket["count"] += 1
        if bucket["sample"] is None:
            bucket["sample"] = {
                "post_id": row.get("post_id"),
                "idx": row.get("idx"),
                "detail": row.get("error_detail"),
                "http_status": row.get("http_status"),
                "source_url": row.get("source_url"),
            }

    return {
        "total": sum(b["count"] for b in grouped.values()),
        "by_class": grouped,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    runtime = request.app.state.runtime
    job_dir = runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")
    state = job_dir.load_state()
    if state is None or state.phase.terminal:
        raise HTTPException(409, "job is not running")

    state.cancel_requested = True
    job_dir.save_state(state)
    job_dir.append_event("warn", "cancellation requested")
    runtime.request_cancel(job_id)
    return {"job_id": job_id, "cancel_requested": True}


@router.post("/jobs/{job_id}/retry-failed")
async def retry_failed(request: Request, job_id: str):
    """Re-run only the retryable failures from a finished batch.

    Terminal classes (expired_url, http_404) are excluded — retrying a
    permanently dead link just burns time and CDN goodwill.
    """
    runtime = request.app.state.runtime
    if runtime.busy:
        raise HTTPException(409, "a batch is already running")
    job_dir = runtime.jobstore.get(job_id)
    if job_dir is None:
        raise HTTPException(404, "no such job")

    new_job_id, count = await runtime.retry_failed(job_id)
    if count == 0:
        return {"job_id": None, "retryable": 0, "message": "nothing retryable"}
    return {"job_id": new_job_id, "retryable": count, "status": "started"}


@router.get("/health/system")
async def system_health(request: Request):
    """Host telemetry: RAM, CPU, disk, and per-worker RSS.

    On an 8 GB box running 3 OCR workers, this is how an impending OOM becomes
    visible before it kills a multi-hour batch.
    """
    runtime = request.app.state.runtime
    settings = request.app.state.settings
    data = snapshot(settings.data_dir, settings.images_dir)
    data["images"] = image_store(settings.images_dir)
    data["scheduler"] = runtime.scheduler.status()
    data["config"] = {
        "ocr_workers": settings.ocr.workers,
        "batch_size": settings.batch_size,
        "memory_limit_mb": settings.ocr.memory_limit_mb,
        "download_concurrency": settings.download.concurrency,
        "engine": settings.ocr.engine_name,
    }
    return data


@router.get("/pipeline")
async def pipeline_status(request: Request):
    """Where the corpus stands: scanned vs total, run progress, verdict split."""
    runtime = request.app.state.runtime
    return await runtime.pipeline_status()


@router.post("/scheduler/pause")
async def pause_scheduler(request: Request):
    request.app.state.runtime.scheduler.pause()
    return {"paused": True}


@router.post("/scheduler/resume")
async def resume_scheduler(request: Request):
    request.app.state.runtime.scheduler.resume()
    return {"paused": False}
