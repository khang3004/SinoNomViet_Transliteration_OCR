"""FastAPI app service (AGENTS.md §2, §3).

Lightweight, **no GPU**. Serves the upload UI, accepts uploads, creates jobs in
the shared SQLite store, and reads back the resulting JSONL. The GPU-bound OCR
runs in the separate ``worker`` service. The app does NOT need any API keys.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

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
        # no-store so browsers always fetch the latest UI (it changes often).
        return HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})


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


@app.get("/jobs/{job_id}/records")
def get_records(job_id: int) -> dict:
    """Return a finished job's JSONL parsed into records, for inline viewing."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=409, detail=f"output not ready (status={job.status})")
    records = []
    with open(job.output_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return {"job_id": job_id, "records": records}


class RecordEdit(BaseModel):
    """Human edits to one record (all optional; only provided fields change)."""

    id: str
    han: str | None = None
    meaning: str | None = None
    entry_no: int | None = None
    page: int | None = None
    entry_meta: dict | None = None
    review_status: str | None = None
    han_bbox: list[float] | None = None
    meaning_bbox: list[float] | None = None


class NewRecord(BaseModel):
    """A user-drawn box → new record on a page."""

    page: int
    han_bbox: list[float]
    meaning_bbox: list[float] | None = None
    han: str = ""
    meaning: str = ""
    image_path: str = ""  # page image; falls back to a same-page record's image


class DeleteRecord(BaseModel):
    id: str


class ReocrRequest(BaseModel):
    """Re-OCR one box region of a page image."""

    image_path: str
    bbox: list[float]
    page: int = 1


def _job_records(job_id: int):
    """Return (job, records list) or raise the appropriate HTTP error."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=409, detail="output not ready")
    with open(job.output_path, encoding="utf-8") as fh:
        return job, [json.loads(ln) for ln in fh if ln.strip()]


def _save_records(job, records) -> None:
    tmp = job.output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        for r in records:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, job.output_path)


@app.post("/jobs/{job_id}/record")
def update_record(job_id: int, edit: RecordEdit) -> dict:
    """Edit-in-place: apply human corrections to one record in the job's JSONL.

    Marks the record ``review_status="verified"`` (unless overridden), keeps the
    raw OCR in ``han_raw``, and rewrites the JSONL atomically.
    """
    job, records = _job_records(job_id)
    target = next((r for r in records if r.get("id") == edit.id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"record {edit.id!r} not found")

    if edit.han is not None:
        target.setdefault("han_raw", target.get("han", ""))
        target["han"] = edit.han
        target["han_chars"] = list(edit.han)
    if edit.meaning is not None:
        target["meaning"] = edit.meaning
    if edit.entry_no is not None:
        target["entry_no"] = edit.entry_no
    if edit.page is not None:
        target["page"] = edit.page
    if edit.entry_meta is not None:
        target.setdefault("entry_meta", {}).update(edit.entry_meta)
    if edit.han_bbox is not None:
        target["han_bbox"] = edit.han_bbox
    if edit.meaning_bbox is not None:
        target["meaning_bbox"] = edit.meaning_bbox
    target["review_status"] = edit.review_status or "verified"

    _save_records(job, records)
    logger.info("Job %d: record %s updated (status=%s).", job_id, edit.id, target["review_status"])
    return {"ok": True, "record": target}


@app.post("/jobs/{job_id}/record/new")
def create_record(job_id: int, new: NewRecord) -> dict:
    """Append a new record for a user-drawn box."""
    job, records = _job_records(job_id)
    prefix = (records[0]["id"].rsplit(".", 2)[0] if records else "HVB_001")
    line_no = max([r["line_no"] for r in records if r.get("page") == new.page] + [0]) + 1
    # Page image: prefer the value the UI sent, else any record on the SAME page,
    # else the first record's image (single-page fallback).
    same_page = next((r for r in records if r.get("page") == new.page), None)
    image_path = new.image_path or (same_page or (records[0] if records else {})).get("image_path", "")
    rec = {
        "id": f"{prefix}.{new.page:03d}.{line_no:02d}",
        "source_doc": records[0].get("source_doc", "") if records else "",
        "page": new.page,
        "line_no": line_no,
        "han": new.han,
        "han_raw": new.han,
        "han_conf": [],
        "phonetic": "",
        "meaning": new.meaning,
        "layout_type": "two_column",
        "image_path": image_path,
        "entry_no": None,
        "entry_meta": {"ngay": "", "to_tap": "", "loai": "", "xuat_xu": "", "de_tai": ""},
        "han_chars": list(new.han),
        "phonetic_per_char": [],
        "source_of": {"han": "ocr", "phonetic": "", "meaning": "manual"},
        "review_status": "pending",
        "han_bbox": new.han_bbox,
        "meaning_bbox": new.meaning_bbox or new.han_bbox,
    }
    records.append(rec)
    _save_records(job, records)
    logger.info("Job %d: created record %s (manual box).", job_id, rec["id"])
    return {"ok": True, "record": rec}


@app.post("/jobs/{job_id}/record/delete")
def delete_record(job_id: int, req: DeleteRecord) -> dict:
    """Remove a record (e.g. a spurious detection)."""
    job, records = _job_records(job_id)
    kept = [r for r in records if r.get("id") != req.id]
    if len(kept) == len(records):
        raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
    _save_records(job, kept)
    logger.info("Job %d: deleted record %s.", job_id, req.id)
    return {"ok": True, "deleted": req.id}


@app.post("/jobs/{job_id}/reocr")
def enqueue_reocr(job_id: int, req: ReocrRequest) -> dict:
    """Enqueue a re-OCR of one box region; the worker (with OCR) runs it."""
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = json.dumps({"image_path": os.path.basename(req.image_path), "bbox": req.bbox})
    rid = store.create(
        filename=f"reocr p{req.page}", input_path="", source_doc="",
        kind="reocr", payload=payload,
    )
    return {"reocr_job_id": rid}


@app.get("/reocr/{rid}")
def get_reocr(rid: int) -> dict:
    """Poll a re-OCR job: returns status, and {text, conf} when done."""
    return _poll_result(rid, "reocr")


class LLMRequest(BaseModel):
    """Bring-your-own-key LLM request for one record."""

    id: str
    provider: str = "gemini"   # gemini | openai | anthropic
    api_key: str               # the USER's own key — used per-request, never stored
    model: str | None = None


@app.get("/llm/providers")
def llm_providers() -> dict:
    """Available LLM providers + their default models (for the UI dropdown)."""
    from pipeline import llm

    return {
        "providers": [
            {"name": n, "default_model": llm.get_provider(n).default_model}
            for n in llm.available()
        ]
    }


@app.post("/jobs/{job_id}/correct")
def correct_record(job_id: int, req: LLMRequest) -> dict:
    """AI-correct one record's Han with the USER's own key (synchronous).

    Uses the raw OCR Han + the entry's Vietnamese meaning as context. The key is
    used only for this call — never persisted, never logged.
    """
    _job, records = _job_records(job_id)
    rec = next((r for r in records if r.get("id") == req.id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
    from pipeline.llm.tasks import correct_han

    try:
        text = correct_han(
            req.provider, req.api_key,
            rec.get("han_raw") or rec.get("han", ""), rec.get("meaning", ""), req.model,
        )
    except Exception as exc:  # noqa: BLE001 - report a clean error to the UI
        raise HTTPException(status_code=400, detail=f"correction failed: {exc}") from exc
    return {"text": text}


@app.post("/jobs/{job_id}/translate")
def translate_record(job_id: int, req: LLMRequest) -> dict:
    """Translate one record's Han to Vietnamese with the USER's own key."""
    _job, records = _job_records(job_id)
    rec = next((r for r in records if r.get("id") == req.id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
    from pipeline.llm.tasks import translate_han

    try:
        text = translate_han(req.provider, req.api_key, rec.get("han", ""), req.model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"translation failed: {exc}") from exc
    return {"text": text}


def _poll_result(rid: int, kind: str) -> dict:
    job = store.get(rid)
    if job is None or job.kind != kind:
        raise HTTPException(status_code=404, detail=f"{kind} job not found")
    out = {"status": job.status}
    if job.status == "done" and job.output_path and os.path.exists(job.output_path):
        with open(job.output_path, encoding="utf-8") as fh:
            out.update(json.load(fh))
    elif job.status == "failed":
        out["error"] = job.error
    return out


@app.get("/pages/{filename}")
def get_page_image(filename: str):
    """Serve a rendered PDF page image (or an uploaded image) for side-by-side
    review. Looks in data/output/pages first, then data/uploads."""
    safe = os.path.basename(filename)  # prevent path traversal
    for base in (os.path.join(config.output_dir, "pages"), config.uploads_dir):
        path = os.path.join(base, safe)
        if os.path.exists(path):
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="page image not found")


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"
