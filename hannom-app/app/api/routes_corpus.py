"""Admin endpoints: load the upstream data, index Drive, export results.

Ingest is deliberately a separate step from upload. Replacing the corpus while
reviewers hold assignments is disruptive, so an admin uploads both files, reads
what the merge would produce, and then commits.
"""

from __future__ import annotations

import csv
import io
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.api.auth import current_user, require_admin
from app.core import corpus, evalsheet
from app.core.uploads import UnknownUploadKind, UploadStore, UploadTooLarge

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

EXPORT_COLUMNS = [
    "post_id", "image", "caption", "ground_truth", "gemini_ocr", "deepseek_ocr",
    "post_link", "band", "verdict", "corrected", "note", "gemini_verdict",
    "ground_truth_cer_accuracy", "ground_truth_max_accuracy",
    "gemini_cer_accuracy", "gemini_max_accuracy",
    "reviewer", "reviewed_at", "seconds_spent",
]


def _uploads(request: Request) -> UploadStore:
    return UploadStore(request.app.state.settings.uploads_dir)


@router.get("/corpus")
async def corpus_status(request: Request, user: dict = Depends(current_user)):
    runtime = request.app.state.runtime
    return {
        "uploads": _uploads(request).describe(),
        "records": len(runtime.audit.corpus.records()),
        "posts": runtime.audit.corpus.post_count(),
        "ingest": runtime.audit.corpus.report(),
        "drive": runtime.drive_index.stats(),
    }


@router.post("/corpus/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """Store one of ``ground_truth.jsonl`` / ``ground_truth.xlsx``."""
    try:
        stored = _uploads(request).save(file.file, file.filename or "")
    except UnknownUploadKind as exc:
        raise HTTPException(400, str(exc)) from exc
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    finally:
        await file.close()
    return stored.to_json()


@router.post("/corpus/ingest")
async def ingest(request: Request, user: dict = Depends(require_admin)):
    """Merge the stored files and replace the corpus snapshot.

    Assignments and reviews are keyed on ``record_id``, which is derived from the
    post id and image index — so a re-ingest of corrected upstream data keeps
    every existing review attached to the right image. Records that vanish from
    the new files keep their reviews on disk but drop out of the queues.
    """
    store = _uploads(request)
    jsonl_path = store.path_for("jsonl")
    xlsx_path = store.path_for("xlsx")
    if not jsonl_path.exists() and not xlsx_path.exists():
        raise HTTPException(400, "Upload ground_truth.jsonl or ground_truth.xlsx first.")

    try:
        records, report = corpus.build(jsonl_path, xlsx_path)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc

    if not records:
        raise HTTPException(
            400,
            "The merge produced no reviewable records. "
            + ", ".join(f"{k}: {v}" for k, v in report.skipped.items())
            or "Check that the files have an 'image' column.",
        )

    runtime = request.app.state.runtime
    runtime.audit.corpus.replace(records, report)
    return {"records": len(records), "ingest": report.to_json()}


@router.post("/drive/refresh")
async def refresh_drive(request: Request, user: dict = Depends(require_admin)):
    """List the shared Drive folder into a local filename -> file-id index."""
    runtime = request.app.state.runtime
    report = await runtime.drive_index.refresh()
    if report.error:
        raise HTTPException(502, report.error)
    return report.to_json()


# --- exports -----------------------------------------------------------

@router.get("/export/reviews.jsonl")
async def export_jsonl(request: Request, user: dict = Depends(current_user)):
    import json

    rows = request.app.state.runtime.audit.export_rows()
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return Response(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="reviews.jsonl"'},
    )


@router.get("/export/reviews.csv")
async def export_csv(request: Request, user: dict = Depends(current_user)):
    rows = request.app.state.runtime.audit.export_rows()
    buffer = io.StringIO()
    # utf-8-sig on the response: Excel opens a plain UTF-8 CSV as mojibake, and
    # every character in this file is one someone transcribed by hand.
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reviews.csv"'},
    )


@router.get("/export/danh_gia.xlsx")
async def export_eval_sheet(request: Request, user: dict = Depends(current_user)):
    """The upstream team's own review format, with the character diff coloured.

    Deliberately not the same numbers as reviews.xlsx: this one uses their
    max-length denominator so it sits alongside their existing sheets, while
    reviews.xlsx reports standard CER. See app/core/evalsheet.py.
    """
    rows = request.app.state.runtime.audit.export_rows()
    buffer = io.BytesIO()
    evalsheet.build(rows).save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": 'attachment; filename="danh_gia.xlsx"'},
    )


@router.get("/export/reviews.xlsx")
async def export_xlsx(request: Request, user: dict = Depends(current_user)):
    try:
        import openpyxl
    except ModuleNotFoundError as exc:  # pragma: no cover - pinned dependency
        raise HTTPException(500, "openpyxl is not installed") from exc

    rows = request.app.state.runtime.audit.export_rows()
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet("reviews")
    sheet.append(EXPORT_COLUMNS)
    for row in rows:
        # Accuracies stay floats with a percent format, not "97.50%" strings —
        # a text column cannot be averaged, which is the first thing anyone
        # does with this sheet.
        sheet.append([row.get(column) for column in EXPORT_COLUMNS])

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": 'attachment; filename="reviews.xlsx"'},
    )
