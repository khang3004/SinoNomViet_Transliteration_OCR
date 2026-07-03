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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from app import auth
from pipeline.config import load_config
from pipeline.jobstore import get_store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("hannom.app")

config = load_config()
for _d in (config.data_dir, config.uploads_dir, config.output_dir):
    os.makedirs(_d, exist_ok=True)
store = get_store(config)

# When set, records live in Postgres (source of truth); when empty, the app
# falls back to the per-job JSONL files (dev / tests, no psycopg needed).
DATABASE_URL = config.database_url

app = FastAPI(title="hannom-app", version="0.1.0")

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX = os.path.join(_HERE, "static", "index.html")


@app.on_event("startup")
def _startup() -> None:
    """Seed the initial admin (if configured) once Postgres is reachable."""
    try:
        auth.seed_admin()
    except Exception:  # noqa: BLE001 - never block startup on seeding
        logger.exception("Admin seeding failed.")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Require a valid session for every non-public route.

    Only active when auth is configured (DATABASE_URL + AUTH_SECRET). Public
    paths (index, healthz, login/logout, static assets) always pass through.
    """
    if not (DATABASE_URL and config.auth_secret) or auth.is_public_path(request.url.path):
        return await call_next(request)
    user = auth.user_from_request(request)
    if user is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    request.state.user = user
    return await call_next(request)


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def auth_login(body: LoginBody) -> JSONResponse:
    """Verify credentials, set an httpOnly session cookie."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="auth requires DATABASE_URL")
    from pipeline.db import users_repo

    u = users_repo.get_by_username(DATABASE_URL, body.username)
    if not u or not auth.verify_password(body.password, u["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = auth.make_token(u)
    resp = JSONResponse({"ok": True, "user": {"username": u["username"], "role": u["role"]}})
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax", max_age=7 * 24 * 3600, path="/"
    )
    return resp


@app.post("/auth/logout")
def auth_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    """Return the logged-in user + their page-range assignments (401 if none)."""
    user = auth.user_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    assignments = []
    if DATABASE_URL and user.get("role") != "admin":
        from pipeline.db import assignments_repo

        assignments = assignments_repo.list_for_user(DATABASE_URL, user["id"])
    return {"user": user, "assignments": assignments}


# --- admin: users + page-range assignments ----------------------------
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "reviewer"


class NewAssignment(BaseModel):
    user_id: int
    job_id: int
    page_start: int
    page_end: int


@app.post("/admin/users")
def admin_create_user(body: NewUser, _admin: dict = Depends(auth.require_admin)) -> dict:
    from pipeline.db import users_repo

    if body.role not in ("admin", "reviewer"):
        raise HTTPException(status_code=400, detail="role must be admin or reviewer")
    if users_repo.get_by_username(DATABASE_URL, body.username):
        raise HTTPException(status_code=409, detail="username already exists")
    u = users_repo.create(
        DATABASE_URL, body.username, auth.hash_password(body.password), body.role
    )
    return {"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"]}}


@app.get("/admin/users")
def admin_list_users(_admin: dict = Depends(auth.require_admin)) -> dict:
    from pipeline.db import users_repo

    return {"users": users_repo.list_users(DATABASE_URL)}


@app.post("/admin/assignments")
def admin_create_assignment(body: NewAssignment, _admin: dict = Depends(auth.require_admin)) -> dict:
    from pipeline.db import assignments_repo

    if store.get(body.job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    a = assignments_repo.create(
        DATABASE_URL, body.user_id, body.job_id, body.page_start, body.page_end
    )
    return {"ok": True, "assignment": a}


@app.get("/admin/assignments")
def admin_list_assignments(_admin: dict = Depends(auth.require_admin)) -> dict:
    from pipeline.db import assignments_repo

    return {"assignments": assignments_repo.list_all(DATABASE_URL)}


@app.delete("/admin/assignments/{assignment_id}")
def admin_delete_assignment(assignment_id: int, _admin: dict = Depends(auth.require_admin)) -> dict:
    from pipeline.db import assignments_repo

    if not assignments_repo.delete(DATABASE_URL, assignment_id):
        raise HTTPException(status_code=404, detail="assignment not found")
    return {"ok": True}


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
    """Download the job's records as JSONL. Regenerated from Postgres (so human
    edits are reflected) when a DB is configured; else the on-disk JSONL file."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if DATABASE_URL:
        from pipeline.db import records_repo

        recs = records_repo.list_by_job(DATABASE_URL, job_id)
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
        fname = os.path.basename(job.output_path) or f"job_{job_id}.jsonl"
        return Response(
            content=body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    if not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=409, detail=f"output not ready (status={job.status})")
    return FileResponse(
        job.output_path,
        media_type="application/x-ndjson",
        filename=os.path.basename(job.output_path),
    )


@app.get("/jobs/{job_id}/records")
def get_records(job_id: int, request: Request) -> dict:
    """Return a finished job's records for the review editor.

    Every record is tagged ``editable`` for the current viewer (view-all,
    edit-own): admins edit all, reviewers only their assigned page ranges.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if DATABASE_URL:
        from pipeline.db import records_repo

        recs = records_repo.list_by_job(DATABASE_URL, job_id)
        user = getattr(request.state, "user", None)
        _annotate_editable(recs, user, job_id)
        return {"job_id": job_id, "records": recs, "role": (user or {}).get("role")}
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


def _editable(user: dict | None, job_id: int, page) -> bool:
    """Whether ``user`` may edit a record on ``page`` of ``job_id``.

    Admins (and the auth-off dev mode, user=None) edit everything; reviewers only
    their assigned page ranges.
    """
    if user is None or user.get("role") == "admin":
        return True
    from pipeline.db import assignments_repo

    return assignments_repo.covers(DATABASE_URL, user["id"], job_id, page)


def _require_can_edit(request: Request, job_id: int, page) -> None:
    user = getattr(request.state, "user", None)
    if not _editable(user, job_id, page):
        raise HTTPException(status_code=403, detail="page not in your assigned range")


def _annotate_editable(recs: list[dict], user: dict | None, job_id: int) -> None:
    """Tag each record with an ``editable`` flag for the current viewer."""
    if user is None or user.get("role") == "admin":
        for r in recs:
            r["editable"] = True
        return
    from pipeline.db import assignments_repo

    ranges = assignments_repo.ranges_for(DATABASE_URL, user["id"], job_id)
    for r in recs:
        p = r.get("page")
        r["editable"] = p is not None and any(lo <= p <= hi for (lo, hi) in ranges)


def _find_record(job_id: int, record_id: str) -> dict:
    """Return one record dict (from Postgres or JSONL) or raise 404."""
    if DATABASE_URL:
        from pipeline.db import records_repo

        rec = records_repo.get(DATABASE_URL, job_id, record_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"record {record_id!r} not found")
        return rec
    _job, records = _job_records(job_id)
    rec = next((r for r in records if r.get("id") == record_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"record {record_id!r} not found")
    return rec


@app.post("/jobs/{job_id}/record")
def update_record(job_id: int, edit: RecordEdit, request: Request) -> dict:
    """Edit-in-place: apply human corrections to one record in the job's JSONL.

    Marks the record ``review_status="verified"`` (unless overridden), keeps the
    raw OCR in ``han_raw``, and rewrites the JSONL atomically.
    """
    if DATABASE_URL:
        from pipeline.db import records_repo

        rec = records_repo.get(DATABASE_URL, job_id, edit.id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"record {edit.id!r} not found")
        _require_can_edit(request, job_id, rec.get("page"))
        changes: dict = {}
        if edit.han is not None:
            changes["han"] = edit.han
            changes["han_chars"] = list(edit.han)
        if edit.meaning is not None:
            changes["meaning"] = edit.meaning
        if edit.entry_no is not None:
            changes["entry_no"] = edit.entry_no
        if edit.page is not None:
            changes["page"] = edit.page
        if edit.entry_meta is not None:
            changes["entry_meta"] = {**(rec.get("entry_meta") or {}), **edit.entry_meta}
        if edit.han_bbox is not None:
            changes["han_bbox"] = edit.han_bbox
        if edit.meaning_bbox is not None:
            changes["meaning_bbox"] = edit.meaning_bbox
        changes["review_status"] = edit.review_status or "verified"
        updated = records_repo.update(DATABASE_URL, job_id, edit.id, changes)
        logger.info("Job %d: record %s updated (status=%s).", job_id, edit.id, changes["review_status"])
        return {"ok": True, "record": updated}

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


def _new_record_dict(prefix, line_no, image_path, source_doc, new: "NewRecord") -> dict:
    """Build a fresh record dict for a user-drawn box (shared DB + JSONL paths)."""
    return {
        "id": f"{prefix}.{new.page:03d}.{line_no:02d}",
        "source_doc": source_doc,
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


@app.post("/jobs/{job_id}/record/new")
def create_record(job_id: int, new: NewRecord, request: Request) -> dict:
    """Append a new record for a user-drawn box."""
    if DATABASE_URL:
        from pipeline.db import records_repo

        if store.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        _require_can_edit(request, job_id, new.page)
        existing = records_repo.list_by_job(DATABASE_URL, job_id)
        prefix = records_repo.id_prefix(DATABASE_URL, job_id)
        line_no = records_repo.next_line_no(DATABASE_URL, job_id, new.page)
        same_page = next((r for r in existing if r.get("page") == new.page), None)
        image_path = new.image_path or (same_page or (existing[0] if existing else {})).get("image_path", "")
        source_doc = existing[0].get("source_doc", "") if existing else ""
        rec = _new_record_dict(prefix, line_no, image_path, source_doc, new)
        created = records_repo.create_one(DATABASE_URL, job_id, rec)
        logger.info("Job %d: created record %s (manual box).", job_id, created["id"])
        return {"ok": True, "record": created}

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
def delete_record(job_id: int, req: DeleteRecord, request: Request) -> dict:
    """Remove a record (e.g. a spurious detection)."""
    if DATABASE_URL:
        from pipeline.db import records_repo

        existing = records_repo.get(DATABASE_URL, job_id, req.id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
        _require_can_edit(request, job_id, existing.get("page"))
        if not records_repo.delete(DATABASE_URL, job_id, req.id):
            raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
        logger.info("Job %d: deleted record %s.", job_id, req.id)
        return {"ok": True, "deleted": req.id}

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
    rec = _find_record(job_id, req.id)
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
    rec = _find_record(job_id, req.id)
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
