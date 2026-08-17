"""Wires the core pipeline to the web layer and owns the single running batch.

Everything stateful lives here so the route modules stay thin. Only one batch
runs at a time by design: OCR already saturates the 4 vCPUs, and concurrent
batches would fight over both CPU and the processed-ids index.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.batch import BatchRunner
from app.core.config import Settings
from app.core.jobstore import JobDir, JobState, JobStore, Phase, new_run_id
from app.core.models import ErrorClass
from app.core.scheduler import BatchScheduler
from app.core.signing import ImageUrlSigner
from app.core.sink import MinioResultSink
from app.core.source import MinioRecordSource
from app.core.storage import MinioStorage

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobstore = JobStore(settings.jobs_dir)

        storage = MinioStorage(settings.minio)
        self.source = MinioRecordSource(settings, storage=storage)
        self.sink = MinioResultSink(settings, storage=storage)
        self.signer = ImageUrlSigner(
            base_url=settings.images.public_base_url,
            secret=settings.images.signing_secret,
            ttl_days=settings.images.ttl_days,
        )
        self.runner = BatchRunner(settings, self.source, self.sink, self.signer)

        self.scheduler = BatchScheduler(
            interval_s=settings.scan_interval_s,
            run_batch=self._scheduled_tick,
            enabled=settings.scheduler_enabled,
        )

        self._current_task: asyncio.Task | None = None
        self._current_job_id: str | None = None
        self._lock = asyncio.Lock()
        self._pipeline_cache: dict[str, Any] = {}
        self._pipeline_cached_at = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        # A container restart leaves jobs stuck mid-phase with nothing driving
        # them; mark those so the UI can offer Resume instead of showing a job
        # that appears to be running forever.
        reaped = self.jobstore.reap_interrupted()
        if reaped:
            log.warning("marked %d interrupted job(s) after restart: %s", len(reaped), reaped)
        self.scheduler.start()

    async def shutdown(self) -> None:
        await self.scheduler.stop()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def busy(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    @property
    def current_job_id(self) -> str | None:
        return self._current_job_id if self.busy else None

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------

    async def start_batch(
        self, limit: int | None = None, confirm_expired: bool = False
    ) -> str:
        async with self._lock:
            if self.busy:
                raise RuntimeError("a batch is already running")
            limit = limit or self.settings.batch_size
            job_dir, state = self.jobstore.create(new_run_id(), limit=limit)
            self._current_job_id = state.job_id
            self._current_task = asyncio.create_task(
                self._run(job_dir, state, confirm_expired), name=f"batch-{state.job_id}"
            )
            return state.job_id

    async def _run(self, job_dir: JobDir, state: JobState, confirm_expired: bool) -> None:
        try:
            await self.runner.run(job_dir, state, confirm_expired=confirm_expired)
        except Exception:  # noqa: BLE001 - already recorded on the job
            log.exception("batch %s crashed", state.job_id)
        finally:
            self.source.invalidate_cache()
            self._pipeline_cached_at = 0.0

    def request_cancel(self, job_id: str) -> None:
        """Cancellation is cooperative: the runner checks the flag between
        items so partial work is still persisted and publishable."""
        job_dir = self.jobstore.get(job_id)
        if job_dir is None:
            return
        state = job_dir.load_state()
        if state is not None:
            state.cancel_requested = True
            job_dir.save_state(state)

    async def _scheduled_tick(self) -> bool:
        """One scheduler iteration. Returns True when a batch actually ran."""
        if self.busy:
            return False
        if not self.settings.minio.endpoint:
            log.debug("scheduler idle: MINIO_ENDPOINT not configured")
            return False

        pending = await asyncio.to_thread(self.source.iter_pending, 1)
        if not pending.posts:
            return False

        # Scheduled runs auto-confirm: a human is not watching, and the preflight
        # numbers are still recorded on the job for later inspection.
        await self.start_batch(confirm_expired=True)
        self.scheduler.last_run_at = time.time()

        if self._current_task is not None:
            await self._current_task
        return True

    async def retry_failed(self, job_id: str) -> tuple[str | None, int]:
        """Start a batch limited to the retryable failures of a prior job."""
        job_dir = self.jobstore.get(job_id)
        if job_dir is None:
            return None, 0

        retryable_posts: set[str] = set()
        for row in job_dir.read_downloads() + job_dir.read_results():
            if row.get("ok"):
                continue
            raw = row.get("error_class")
            if not raw:
                continue
            try:
                if ErrorClass(raw).retryable:
                    retryable_posts.add(str(row.get("post_id")))
            except ValueError:
                continue

        if not retryable_posts:
            return None, 0

        # Un-mark them so the normal claim path picks them up again.
        await asyncio.to_thread(self._unmark_posts, retryable_posts)
        new_job_id = await self.start_batch(
            limit=len(retryable_posts), confirm_expired=True
        )
        return new_job_id, len(retryable_posts)

    def _unmark_posts(self, post_ids: set[str]) -> None:
        """Drop post_ids from the processed index so they can be reclaimed."""
        cfg = self.settings.minio
        storage = self.source.storage
        rows = [
            row
            for row in storage.iter_jsonl(cfg.processed_ids_key)
            if str(row.get("post_id")) not in post_ids
        ]
        storage.put_jsonl(cfg.processed_ids_key, rows)
        self.source.invalidate_cache()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    async def pipeline_status(self) -> dict[str, Any]:
        """Corpus-level progress. Cached briefly — it costs several MinIO reads
        and the UI polls every couple of seconds."""
        if time.time() - self._pipeline_cached_at < 15 and self._pipeline_cache:
            return self._pipeline_cache

        try:
            counts = await asyncio.to_thread(self.sink.counts)
            processed = await asyncio.to_thread(self.source.processed_ids)
            corpus_total = await asyncio.to_thread(self.source.corpus_total)
            run_ids = await asyncio.to_thread(self.source.list_run_ids)
            connected = True
            error = ""
        except Exception as exc:  # noqa: BLE001 - MinIO down should not 500 the UI
            log.warning("pipeline status unavailable: %s", exc)
            counts = {"han_valid": 0, "han_invalid": 0, "failed": 0}
            processed, corpus_total, run_ids = set(), 0, []
            connected = False
            error = f"{type(exc).__name__}: {exc}"

        active = self.jobstore.active()
        payload = {
            "connected": connected,
            "error": error,
            "corpus_total": corpus_total,
            "processed_total": len(processed),
            "remaining": max(0, corpus_total - len(processed)),
            "percent": round(len(processed) / corpus_total * 100, 1) if corpus_total else 0.0,
            "han_valid": counts["han_valid"],
            "han_invalid": counts["han_invalid"],
            "failed": counts["failed"],
            "crawl_runs": run_ids,
            "crawl_runs_total": len(run_ids),
            "active_job": active.to_json() if active else None,
            "busy": self.busy,
        }
        self._pipeline_cache = payload
        self._pipeline_cached_at = time.time()
        return payload
