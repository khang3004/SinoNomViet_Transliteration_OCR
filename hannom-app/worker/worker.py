"""Worker entrypoint (AGENTS.md §2, §3, §7).

The GPU-bearing service. Polls the SQLite job store for pending jobs and runs the
pipeline on each, writing JSONL into the shared ``data/output`` volume. API keys
are read from the environment (worker only); on startup it logs ONLY whether each
key is present (never the value) and fails fast if a selected ``*_BACKEND=api``
lacks its key.

Run:  python -m worker.worker
"""

from __future__ import annotations

import json
import logging
import os
import time

from pipeline import ocr
from pipeline.config import load_config
from pipeline.jobstore import get_store
from pipeline.runner import process_file, reocr_region

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hannom.worker")

POLL_INTERVAL_S = float(os.environ.get("WORKER_POLL_S", "2.0"))


def _ensure_dirs(config) -> None:
    for d in (config.data_dir, config.uploads_dir, config.output_dir, config.work_dir):
        os.makedirs(d, exist_ok=True)


def run_once(store, config, engine) -> bool:
    """Claim and process one job. Returns True if a job was handled."""
    job = store.claim_next()
    if job is None:
        return False
    try:
        if job.kind == "reocr":
            _run_reocr(store, config, engine, job)
        else:
            _run_extract(store, config, engine, job)
    except Exception as exc:  # noqa: BLE001 - record failure, keep the worker alive
        logger.exception("Job %d (%s) failed.", job.id, job.kind)
        store.mark_failed(job.id, str(exc))
    return True


def _run_extract(store, config, engine, job) -> None:
    logger.info("Claimed extract job %d (%s).", job.id, job.filename)
    out_name = f"job_{job.id}_{os.path.splitext(job.filename)[0]}.jsonl"
    out_path = os.path.join(config.output_dir, out_name)
    records = process_file(job.input_path, out_path, config, source_doc=job.source_doc, engine=engine)
    # When Postgres is configured it is the source of truth for the review editor;
    # persist the extracted records there (the JSONL at out_path is kept as an
    # export/debug artifact). Without a DB, the JSONL alone is used (dev/tests).
    if getattr(config, "database_url", ""):
        from pipeline.db.records_repo import insert_many

        n = insert_many(config.database_url, job.id, [r.to_dict() for r in records])
        logger.info("Job %d: inserted %d record(s) into Postgres.", job.id, n)
    store.mark_done(job.id, out_path)
    logger.info("Job %d done: %d record(s) → %s", job.id, len(records), out_path)


# A SECOND OCR engine tuned for Vietnamese (Latin + diacritics), built lazily on
# the first Việt re-OCR so it costs no memory until actually used. The default Hán
# engine (chinese_cht) cannot read Vietnamese.
_VN_ENGINE = None
_VN_ENGINE_TRIED = False


def _vn_engine(config):
    """Return a Vietnamese OCR engine (paddle lang=vi), or None if unavailable."""
    global _VN_ENGINE, _VN_ENGINE_TRIED
    if _VN_ENGINE is not None or _VN_ENGINE_TRIED:
        return _VN_ENGINE
    _VN_ENGINE_TRIED = True
    if config.ocr_backend != "paddle":
        return None
    try:
        from pipeline.ocr.paddle import PaddleEngine

        lang = os.environ.get("OCR_LANG_VN", "vi")
        logger.info("Building Vietnamese OCR engine (lang=%s) for Việt re-OCR …", lang)
        _VN_ENGINE = PaddleEngine(lang=lang)
        logger.info("Vietnamese OCR engine ready.")
    except Exception:  # noqa: BLE001 - fall back to the Hán engine if it can't build
        logger.exception("Could not build the Vietnamese OCR engine; Việt re-OCR will be poor.")
    return _VN_ENGINE


def _run_reocr(store, config, engine, job) -> None:
    """Re-OCR one box region; write {text, conf} JSON for the app to poll."""
    args = json.loads(job.payload or "{}")
    img_name = args.get("image_path") or args.get("page_image") or ""
    page_image = os.path.join(config.output_dir, "pages", os.path.basename(img_name))
    field = args.get("field", "han")
    # Vietnamese uses its own engine; Hán uses the warm default engine.
    ocr_engine = (_vn_engine(config) or engine) if field == "meaning" else engine
    logger.info("Claimed reocr job %d (%s bbox=%s field=%s).", job.id, os.path.basename(page_image), args.get("bbox"), field)
    result = reocr_region(page_image, args["bbox"], config, engine=ocr_engine, field=field)
    out_path = os.path.join(config.output_dir, "reocr", f"reocr_{job.id}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    store.mark_done(job.id, out_path)
    logger.info("Reocr job %d done: %r (conf=%.2f)", job.id, result["text"], result["conf"])


def main() -> None:
    config = load_config()
    _ensure_dirs(config)

    logger.info("Worker starting. Config: %s", config.summary())
    config.log_key_presence()  # booleans only — never key values
    config.validate()  # fail fast if a selected api backend lacks its key

    # Build the OCR engine ONCE and keep it warm for both extract & reocr jobs.
    engine = ocr.get_engine(config.ocr_backend)
    logger.info("OCR engine %r loaded.", config.ocr_backend)

    store = get_store(config)
    requeued = store.requeue_running()
    if requeued:
        logger.info("Requeued %d orphaned 'running' job(s) from a prior worker.", requeued)
    logger.info("Polling for jobs every %.1fs …", POLL_INTERVAL_S)
    while True:
        try:
            handled = run_once(store, config, engine)
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Unexpected error in worker loop.")
            handled = False
        if not handled:
            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
