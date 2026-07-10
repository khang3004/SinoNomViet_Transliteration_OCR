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
import time

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
        auth.COOKIE_NAME, token, httponly=True, samesite="lax", secure=config.cookie_secure,
        max_age=7 * 24 * 3600, path="/",
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


class PasswordReset(BaseModel):
    password: str


@app.post("/admin/users/{user_id}/password")
def admin_set_password(
    user_id: int, body: PasswordReset, _admin: dict = Depends(auth.require_admin)
) -> dict:
    """Admin resets a user's password (bcrypt-hashed; plaintext never stored)."""
    from pipeline.db import users_repo

    if not body.password.strip():
        raise HTTPException(status_code=400, detail="password cannot be empty")
    if not users_repo.set_password(DATABASE_URL, user_id, auth.hash_password(body.password)):
        raise HTTPException(status_code=404, detail="user not found")
    logger.info("Admin reset password for user %d.", user_id)
    return {"ok": True}


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


@app.get("/admin/progress")
def admin_progress(_admin: dict = Depends(auth.require_admin)) -> dict:
    """Per-reviewer review progress (verified/total, current page, last active)."""
    from pipeline.db import assignments_repo

    return {"progress": assignments_repo.progress(DATABASE_URL)}


# --- corpus view (whole corpus by Trang số, across all uploads) --------
def _member_filter(request: Request, member: int | None) -> int | None:
    """Honor the ?member= filter only for admins; reviewers get the aggregate."""
    user = getattr(request.state, "user", None)
    if member and user and user.get("role") == "admin":
        return member
    return None


@app.get("/corpus/pages")
def corpus_pages(
    request: Request, member: int | None = None, offset: int = 0, limit: int = 50
) -> dict:
    """Paginated list of DONE pages (Trang số with verified work) across all jobs."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="corpus requires DATABASE_URL")
    from pipeline.db import corpus_repo

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows, total = corpus_repo.page_index(
        DATABASE_URL, _member_filter(request, member), offset, limit
    )
    return {"pages": rows, "total": total, "offset": offset, "limit": limit}


@app.get("/corpus/summary")
def corpus_summary(request: Request, member: int | None = None) -> dict:
    """Corpus totals (done pages, entries, verified) for the header."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="corpus requires DATABASE_URL")
    from pipeline.db import corpus_repo

    return corpus_repo.summary(DATABASE_URL, _member_filter(request, member))


@app.get("/corpus/page/{page}")
def corpus_page(page: int, request: Request) -> dict:
    """The merged entries on one Trang số across all jobs, for reading."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="corpus requires DATABASE_URL")
    from pipeline.db import corpus_repo

    entries = corpus_repo.page_entries(DATABASE_URL, page)
    user = getattr(request.state, "user", None)
    editable = False
    if user and user.get("role") == "admin":
        editable = True
    elif user and entries:
        from pipeline.db import assignments_repo

        job_ids = {e["job_id"] for e in entries}
        editable = any(
            assignments_repo.covers(DATABASE_URL, user["id"], jid, page) for jid in job_ids
        )
    return {"page": page, "entries": entries, "editable": editable}


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
    request: Request,
    file: UploadFile = File(...),
    source_doc: str = Form(""),
) -> JSONResponse:
    """Accept an image/PDF upload and enqueue a processing job (admin only)."""
    user = getattr(request.state, "user", None)
    if user is not None and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only admins can upload")
    filename = os.path.basename(file.filename or "upload")
    dest = os.path.join(config.uploads_dir, filename)
    # Avoid clobbering: prefix with a counter if needed.
    dest = _unique_path(dest)
    with open(dest, "wb") as out:
        out.write(await file.read())
    job_id = store.create(filename=os.path.basename(dest), input_path=dest, source_doc=source_doc)
    logger.info("Enqueued job %d for %s", job_id, dest)
    return JSONResponse({"job_id": job_id, "filename": os.path.basename(dest)})


def _assigned_job_ids(user: dict | None) -> set[int] | None:
    """Job ids a reviewer is assigned to (None when auth/DB is off = full access)."""
    if not (DATABASE_URL and user):
        return None
    from pipeline.db import assignments_repo

    return assignments_repo.job_ids_for_user(DATABASE_URL, user["id"])


def _require_job_access(request: Request, job_id: int) -> None:
    """Reviewers may only touch jobs they're assigned to (admins/dev: all)."""
    user = getattr(request.state, "user", None)
    if user is None or user.get("role") == "admin":
        return
    if job_id not in (_assigned_job_ids(user) or set()):
        raise HTTPException(status_code=403, detail="not assigned to this job")


@app.get("/jobs")
def list_jobs(request: Request) -> dict:
    """List jobs. Reviewers see ONLY the jobs they're assigned to; admins see all."""
    jobs = store.list_jobs()
    user = getattr(request.state, "user", None)
    if user is not None and user.get("role") != "admin":
        allowed = _assigned_job_ids(user) or set()
        jobs = [j for j in jobs if j.id in allowed]
    return {"jobs": [j.__dict__ for j in jobs]}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    _require_job_access(request, job_id)
    return job.__dict__


def _delete_job_files(job) -> None:
    """Best-effort removal of a job's files: uploaded source, output JSONL, and
    rendered page images (pages/job_<id>_*). Frees disk when a job is deleted."""
    import glob

    for path in (job.input_path, job.output_path):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    for p in glob.glob(os.path.join(config.output_dir, "pages", f"job_{job.id}_*")):
        try:
            os.remove(p)
        except OSError:
            pass


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int, request: Request) -> dict:
    """Delete a job and everything under it (admin only).

    Its records + assignments cascade in Postgres; its files are removed from disk.
    """
    user = getattr(request.state, "user", None)
    if user is not None and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only admins can delete jobs")
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    _delete_job_files(job)
    store.delete(job_id)
    logger.info("Deleted job %d (%s).", job_id, job.filename)
    return {"ok": True, "deleted": job_id}


@app.get("/jobs/{job_id}/output")
def get_output(job_id: int, request: Request):
    """Download the job's records as JSONL. Regenerated from Postgres (so human
    edits are reflected) when a DB is configured; else the on-disk JSONL file."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    _require_job_access(request, job_id)
    if DATABASE_URL:
        from pipeline.db import records_repo

        # Export merged entries: a bài spanning a page break becomes ONE line with
        # its parts' han/meaning concatenated (continuations folded into the head).
        recs = records_repo.merged_entries(DATABASE_URL, job_id)
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
    _require_job_access(request, job_id)
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
    field: str = "han"   # "han" (CJK-filtered) or "meaning" (Vietnamese/Latin)


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
        # Record WHO verified and WHEN (for the admin progress dashboard).
        if changes["review_status"] == "verified":
            changes["reviewed_by"] = (getattr(request.state, "user", None) or {}).get("id")
            changes["reviewed_at"] = time.time()
        else:
            changes["reviewed_by"] = None
            changes["reviewed_at"] = None
        updated = records_repo.update(DATABASE_URL, job_id, edit.id, changes)
        # If this is a spanning bài, share the same review decision with its
        # continuation fragment(s) so the Corpus doesn't show them as 'pending'.
        n = records_repo.cascade_status_to_parts(
            DATABASE_URL, job_id, edit.id,
            changes["review_status"], changes.get("reviewed_by"), changes.get("reviewed_at"),
        )
        logger.info("Job %d: record %s updated (status=%s%s).", job_id, edit.id,
                    changes["review_status"], f", {n} continuation(s)" if n else "")
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


class LinkRecord(BaseModel):
    """Mark record ``id`` as a continuation of a head entry on a previous page."""

    id: str
    head_id: str | None = None  # explicit head; if omitted, the previous entry


@app.post("/jobs/{job_id}/record/link")
def link_record(job_id: int, req: LinkRecord, request: Request) -> dict:
    """Link a fragment as the continuation of the entry before it (spanning page)."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="linking requires DATABASE_URL")
    from pipeline.db import records_repo

    tail = records_repo.get(DATABASE_URL, job_id, req.id)
    if tail is None:
        raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
    _require_can_edit(request, job_id, tail.get("page"))
    if req.head_id:
        head = records_repo.get(DATABASE_URL, job_id, req.head_id)
        if head is None:
            raise HTTPException(status_code=404, detail="head entry not found")
        head_id = head.get("part_of") or req.head_id  # resolve to the true head
    else:
        head_id = records_repo.previous_entry_head(
            DATABASE_URL, job_id, tail.get("page") or 0, tail.get("line_no") or 0
        )
        if not head_id:
            raise HTTPException(status_code=400, detail="no previous entry to link to")
    if head_id == req.id:
        raise HTTPException(status_code=400, detail="a record cannot continue itself")
    updated = records_repo.link_as_continuation(DATABASE_URL, job_id, req.id, head_id)
    logger.info("Job %d: linked %s as continuation of %s (inherited head metadata).",
                job_id, req.id, head_id)
    return {"ok": True, "record": updated}


@app.post("/jobs/{job_id}/record/unlink")
def unlink_record(job_id: int, req: DeleteRecord, request: Request) -> dict:
    """Detach a continuation so it is a standalone entry again."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="linking requires DATABASE_URL")
    from pipeline.db import records_repo

    rec = records_repo.get(DATABASE_URL, job_id, req.id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"record {req.id!r} not found")
    _require_can_edit(request, job_id, rec.get("page"))
    updated = records_repo.set_part_of(DATABASE_URL, job_id, req.id, None)
    return {"ok": True, "record": updated}


@app.post("/jobs/{job_id}/reocr")
def enqueue_reocr(job_id: int, req: ReocrRequest) -> dict:
    """Enqueue a re-OCR of one box region; the worker (with OCR) runs it."""
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = json.dumps({
        "image_path": os.path.basename(req.image_path), "bbox": req.bbox,
        "field": req.field if req.field in ("han", "meaning") else "han",
    })
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
            {
                "name": n,
                "default_model": llm.get_provider(n).default_model,
                "supports_vision": getattr(llm.get_provider(n), "supports_vision", True),
                "suggested_models": getattr(llm.get_provider(n), "suggested_models", []),
            }
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


class VisionRequest(BaseModel):
    """Vision LLM read of a cropped box (bring-your-own-key)."""

    id: str = ""               # record id (for logging only)
    provider: str = "gemini"
    api_key: str               # the USER's own key — used per-request, never stored
    model: str | None = None
    image_b64: str             # base64 PNG of the Hán crop (may be a data URL)
    ocr_text: str = ""         # current OCR text (may be blank/wrong)
    vi_image_b64: str = ""     # optional base64 PNG of the parallel Vietnamese crop
    meaning: str = ""          # optional Vietnamese text of the entry (extra context)


@app.post("/jobs/{job_id}/vision_correct")
def vision_correct(job_id: int, req: VisionRequest) -> dict:
    """Read Han from a cropped box IMAGE with the USER's own multimodal key.

    The browser crops the Hán box (and optionally the parallel Vietnamese box) and
    sends the PNG(s); we forward them + the current OCR text + the Vietnamese meaning
    to the chosen provider. Key is used only for this call — never stored.
    """
    import base64

    from pipeline.llm.tasks import vision_read_han

    def _decode(b64: str) -> bytes | None:
        if not b64:
            return None
        return base64.b64decode(b64.split(",", 1)[-1])

    try:
        img = _decode(req.image_b64)
        vi_img = _decode(req.vi_image_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid image data: {exc}") from exc
    if not img:
        raise HTTPException(status_code=400, detail="empty image")
    try:
        text = vision_read_han(
            req.provider, req.api_key, img, req.ocr_text, req.model,
            vi_image=vi_img, meaning=req.meaning,
        )
    except Exception as exc:  # noqa: BLE001 - clean error to the UI
        raise HTTPException(status_code=400, detail=f"vision read failed: {exc}") from exc
    return {"text": text}


class LlmOcrRequest(BaseModel):
    """LLM-as-OCR of one entry from both crops (bring-your-own-key)."""

    id: str = ""
    provider: str = "gemini"
    api_key: str
    model: str | None = None
    han_b64: str = ""          # base64 PNG of the Hán crop
    vi_b64: str = ""           # base64 PNG of the Vietnamese crop (optional)
    han_text: str = ""         # existing Hán text (weak hint only)
    vi_text: str = ""          # existing Vietnamese text (weak hint only)


@app.post("/jobs/{job_id}/llm_ocr")
def llm_ocr_route(job_id: int, req: LlmOcrRequest) -> dict:
    """Transcribe both crops with a multimodal LLM; return {han, meaning}.

    The LLM does the OCR (reads straight from the images). The reviewer's own key
    is used once, never stored.
    """
    import base64

    from pipeline.llm.tasks import llm_ocr as run_llm_ocr

    def _decode(b64: str) -> bytes | None:
        return base64.b64decode(b64.split(",", 1)[-1]) if b64 else None

    try:
        han_img = _decode(req.han_b64)
        vi_img = _decode(req.vi_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid image data: {exc}") from exc
    if not han_img:
        raise HTTPException(status_code=400, detail="empty Hán image")
    try:
        return run_llm_ocr(
            req.provider, req.api_key, han_img, vi_img, req.han_text, req.vi_text, req.model
        )
    except Exception as exc:  # noqa: BLE001 - clean error to the UI
        raise HTTPException(status_code=400, detail=f"LLM OCR failed: {exc}") from exc


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
