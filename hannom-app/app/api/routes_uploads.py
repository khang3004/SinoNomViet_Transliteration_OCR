"""Uploading valid_post.jsonl and downloading results.

This is the default flow: it needs no network path into the k3s cluster, so it
works regardless of how MinIO is exposed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.auth import current_user
from app.core.uploads import UploadTooLarge

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


@router.post("/uploads")
async def create_upload(request: Request, file: UploadFile = File(...)):
    """Store an uploaded valid_post.jsonl and report what is in it.

    The response is the pre-scan summary — how many posts are scannable, how many
    the checkpoint has already seen, and how many images that leaves. Uploading
    the cumulative export a second time should show most posts already processed.
    """
    runtime = request.app.state.runtime

    if not file.filename or not file.filename.endswith((".jsonl", ".json", ".ndjson")):
        raise HTTPException(400, "expected a .jsonl file")

    try:
        upload = await run_in_thread(runtime.uploads.save, file.file, file.filename)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("upload failed")
        raise HTTPException(500, f"could not store upload: {exc}") from exc

    runtime.active_upload_id = upload.upload_id
    stats = await run_in_thread(runtime.upload_stats, upload.upload_id)

    if stats.get("scannable_posts", 0) == 0:
        # Parsed fine but nothing usable — almost always the wrong file or a
        # different schema, and far better to say so now than to "succeed"
        # with an empty batch.
        log.warning("upload %s contained no scannable posts", upload.upload_id)

    return stats


@router.get("/uploads")
async def list_uploads(request: Request):
    runtime = request.app.state.runtime
    return {
        "uploads": [u.to_json() for u in runtime.uploads.list()],
        "active": runtime.active_upload_id,
    }


@router.get("/uploads/current")
async def current_upload(request: Request):
    return request.app.state.runtime.upload_stats()


@router.delete("/uploads/{upload_id}")
async def delete_upload(request: Request, upload_id: str):
    runtime = request.app.state.runtime
    if runtime.busy:
        raise HTTPException(409, "cannot delete while a batch is running")
    if not runtime.uploads.delete(upload_id):
        raise HTTPException(404, "no such upload")
    if runtime.active_upload_id == upload_id:
        latest = runtime.uploads.latest()
        runtime.active_upload_id = latest.upload_id if latest else None
    return {"deleted": upload_id}


@router.get("/results/{name}")
async def download_result(request: Request, name: str):
    """Serve a results file.

    ``name`` is resolved through an allowlist rather than joined onto a path —
    it arrives from a URL.
    """
    runtime = request.app.state.runtime
    path = runtime.file_sink.path_for(name)
    if path is None:
        raise HTTPException(404, "unknown result file")
    if not path.exists():
        raise HTTPException(404, f"{name} has not been produced yet")

    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=name,
        headers={"Cache-Control": "no-store"},
    )


async def run_in_thread(fn, *args):
    import asyncio

    return await asyncio.to_thread(fn, *args)
