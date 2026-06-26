"""Worker entrypoint (AGENTS.md §2, §3, §7).

The GPU-bearing service. Polls the SQLite job store for pending jobs and runs the
pipeline on each, writing JSONL into the shared ``data/output`` volume. API keys
are read from the environment (worker only); on startup it logs ONLY whether each
key is present (never the value) and fails fast if a selected ``*_BACKEND=api``
lacks its key.

Run:  python -m worker.worker
"""

from __future__ import annotations

import logging
import os
import time

from pipeline.config import load_config
from pipeline.jobstore import JobStore
from pipeline.runner import process_file

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hannom.worker")

POLL_INTERVAL_S = float(os.environ.get("WORKER_POLL_S", "2.0"))


def _ensure_dirs(config) -> None:
    for d in (config.data_dir, config.uploads_dir, config.output_dir, config.work_dir):
        os.makedirs(d, exist_ok=True)


def run_once(store: JobStore, config) -> bool:
    """Claim and process one job. Returns True if a job was handled."""
    job = store.claim_next()
    if job is None:
        return False
    logger.info("Claimed job %d (%s).", job.id, job.filename)
    out_name = f"job_{job.id}_{os.path.splitext(job.filename)[0]}.jsonl"
    out_path = os.path.join(config.output_dir, out_name)
    try:
        records = process_file(job.input_path, out_path, config, source_doc=job.source_doc)
        store.mark_done(job.id, out_path)
        logger.info("Job %d done: %d record(s) → %s", job.id, len(records), out_path)
    except Exception as exc:  # noqa: BLE001 - record failure, keep the worker alive
        logger.exception("Job %d failed.", job.id)
        store.mark_failed(job.id, str(exc))
    return True


def main() -> None:
    config = load_config()
    _ensure_dirs(config)

    logger.info("Worker starting. Config: %s", config.summary())
    config.log_key_presence()  # booleans only — never key values
    config.validate()  # fail fast if a selected api backend lacks its key

    store = JobStore(config.jobs_db)
    requeued = store.requeue_running()
    if requeued:
        logger.info("Requeued %d orphaned 'running' job(s) from a prior worker.", requeued)
    logger.info("Polling for jobs every %.1fs …", POLL_INTERVAL_S)
    while True:
        try:
            handled = run_once(store, config)
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Unexpected error in worker loop.")
            handled = False
        if not handled:
            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
