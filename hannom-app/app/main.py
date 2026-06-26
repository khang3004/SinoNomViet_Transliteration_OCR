"""FastAPI app service (AGENTS.md §2, §3).

Lightweight, **no GPU**. Serves the upload UI, accepts uploads, creates jobs in
the shared SQLite store, and reads back the resulting JSONL. The GPU-bound OCR
runs in the separate ``worker`` service. The app does NOT need any API keys.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pipeline.config import load_config
from pipeline.jobstore import JobStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("hannom.app")

config = load_config()
for _d in (config.data_dir, config.uploads_dir, config.output_dir):
    os.makedirs(_d, exist_ok=True)
store = JobStore(config.jobs_db)

app = FastAPI(title="hannom-app", version="0.1.0")

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX = os.path.join(_HERE, "static", "index.html")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(_INDEX, encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "app"}


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    source_doc: str = Form(""),
) -> JSONResponse:
    """Accept an image/PDF upload and enqueue a processing job."""
    filename = os.path.basename(file.filename or "upload")
    dest = os.path.join(config.uploads_dir, filename)
    # Avoid clobbering: prefix with a counter if needed.
    dest = _unique_path(dest)
    with open(dest, "wb") as out:
        out.write(await file.read())
    job_id = store.create(filename=os.path.basename(dest), input_path=dest, source_doc=source_doc)
    logger.info("Enqueued job %d for %s", job_id, dest)
    return JSONResponse({"job_id": job_id, "filename": os.path.basename(dest)})


@app.get("/jobs")
def list_jobs() -> dict:
    jobs = store.list_jobs()
    return {"jobs": [j.__dict__ for j in jobs]}


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.__dict__


@app.get("/jobs/{job_id}/output")
def get_output(job_id: int):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=409, detail=f"output not ready (status={job.status})")
    return FileResponse(
        job.output_path,
        media_type="application/x-ndjson",
        filename=os.path.basename(job.output_path),
    )


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"
