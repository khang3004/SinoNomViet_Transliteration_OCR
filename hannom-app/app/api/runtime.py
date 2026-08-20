"""Wires the core pipeline to the web layer and owns the single running batch.

Two input paths are supported and either can drive a batch:

``upload``  the default — an uploaded ``valid_post.jsonl`` plus a local
            checkpoint. Needs no network path into the k3s cluster.
``minio``   only when ``MINIO_ENDPOINT`` and ``MINIO_GROUP_PREFIX`` are both set.

Results always go to local files (downloadable from the dashboard); when MinIO is
configured they are mirrored there too. Nothing MinIO-related is constructed
unless it is configured, so an unset endpoint is inert rather than fatal.

Only one batch runs at a time: concurrent batches would fight over the shared
checkpoint and the CDN rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.batch import BatchRunner
from app.core.checkpoint import ProcessedCheckpoint
from app.core.config import Settings
from app.core.gallery import Gallery
from app.core.jobstore import JobDir, JobState, JobStore, new_run_id
from app.core.models import ErrorClass
from app.core.scheduler import BatchScheduler
from app.core.signing import ImageUrlSigner
from app.core.sink import FileResultSink, MinioResultSink, TeeResultSink
from app.core.source import FileRecordSource, MinioRecordSource
from app.core.storage import MinioStorage
from app.core.uploads import UploadStore

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobstore = JobStore(settings.jobs_dir)
        self.uploads = UploadStore(settings.uploads_dir)
        self.checkpoint = ProcessedCheckpoint(settings.checkpoint_path)

        self.file_sink = FileResultSink(settings.results_dir)
        self.signer = ImageUrlSigner(
            base_url=settings.images.public_base_url,
            secret=settings.images.signing_secret,
            ttl_days=settings.images.ttl_days,
        )
        # Reads the same export the dashboard offers for download, and re-signs
        # image URLs so thumbnails keep working past the stored expiry.
        self.gallery = Gallery(self.file_sink.ready_for_ocr_path, self.signer)

        # MinIO objects are only built when configured — an unset endpoint must
        # be inert, not a crash on the first attribute access.
        self.minio_source: MinioRecordSource | None = None
        self.minio_sink: MinioResultSink | None = None
        if settings.minio_enabled:
            storage = MinioStorage(settings.minio)
            self.minio_source = MinioRecordSource(settings, storage=storage)
            self.minio_sink = MinioResultSink(settings, storage=storage)
            log.info("MinIO configured: %s", settings.minio.endpoint)
        else:
            log.info("MinIO not configured — upload/download mode only")

        self.scheduler = BatchScheduler(
            interval_s=settings.scan_interval_s,
            run_batch=self._scheduled_tick,
            # Only useful when MinIO can be polled; uploads are user-driven.
            enabled=settings.scheduler_enabled and settings.minio_enabled,
        )

        self._current_task: asyncio.Task | None = None
        self._current_job_id: str | None = None
        self._lock = asyncio.Lock()
        self._pipeline_cache: dict[str, Any] = {}
        self._pipeline_cached_at = 0.0
        self.active_upload_id: str | None = None

    # ------------------------------------------------------------------
    # source / sink selection
    # ------------------------------------------------------------------

    @property
    def minio_available(self) -> bool:
        return self.minio_source is not None

    def build_sink(self):
        if self.minio_sink is not None:
            return TeeResultSink(self.file_sink, self.minio_sink)
        return self.file_sink

    def build_source(self, mode: str, upload_id: str | None = None):
        """Pick an input. Raises with a usable message rather than a 500."""
        if mode == "minio":
            if self.minio_source is None:
                raise RuntimeError(
                    "MinIO is not configured. Set MINIO_ENDPOINT and "
                    "MINIO_GROUP_PREFIX, or use the upload flow."
                )
            return self.minio_source

        upload = (
            self.uploads.get(upload_id) if upload_id else self.uploads.latest()
        )
        if upload is None:
            raise RuntimeError(
                "No valid_post.jsonl uploaded yet. Upload one from the dashboard."
            )
        self.active_upload_id = upload.upload_id
        return FileRecordSource(upload.path, self.checkpoint, self.settings)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        for directory in (
            self.settings.data_dir, self.settings.images_dir, self.settings.jobs_dir,
            self.settings.state_dir, self.settings.uploads_dir, self.settings.results_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        reaped = self.jobstore.reap_interrupted()
        if reaped:
            log.warning("marked %d interrupted job(s) after restart: %s", len(reaped), reaped)

        latest = self.uploads.latest()
        if latest is not None:
            self.active_upload_id = latest.upload_id

        # Starting the scheduler is harmless when disabled; it just idles.
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
        self,
        limit: int | None = None,
        confirm_expired: bool = False,
        mode: str = "upload",
        upload_id: str | None = None,
    ) -> str:
        async with self._lock:
            if self.busy:
                raise RuntimeError("a batch is already running")

            source = self.build_source(mode, upload_id)
            sink = self.build_sink()
            runner = BatchRunner(self.settings, source, sink, self.signer)

            limit = limit or self.settings.batch_size
            job_dir, state = self.jobstore.create(new_run_id(), limit=limit)
            self._current_job_id = state.job_id
            self._current_task = asyncio.create_task(
                self._run(runner, source, job_dir, state, confirm_expired),
                name=f"batch-{state.job_id}",
            )
            return state.job_id

    async def _run(self, runner, source, job_dir: JobDir, state: JobState,
                   confirm_expired: bool) -> None:
        try:
            await runner.run(job_dir, state, confirm_expired=confirm_expired)
        except Exception:  # noqa: BLE001 - already recorded on the job
            log.exception("batch %s crashed", state.job_id)
        finally:
            if hasattr(source, "invalidate_cache"):
                source.invalidate_cache()
            self.checkpoint.invalidate()
            self.gallery.invalidate()
            self._pipeline_cached_at = 0.0

    def request_cancel(self, job_id: str) -> None:
        """Cooperative: the runner checks between items, so partial work is
        still persisted and publishable."""
        job_dir = self.jobstore.get(job_id)
        if job_dir is None:
            return
        state = job_dir.load_state()
        if state is not None:
            state.cancel_requested = True
            job_dir.save_state(state)

    async def _scheduled_tick(self) -> bool:
        if self.busy or self.minio_source is None:
            return False

        pending = await asyncio.to_thread(self.minio_source.iter_pending, 1)
        if not pending.posts:
            return False

        # Nobody is watching a scheduled run, so it auto-confirms; the preflight
        # numbers are still recorded on the job for later inspection.
        await self.start_batch(confirm_expired=True, mode="minio")
        self.scheduler.last_run_at = time.time()

        if self._current_task is not None:
            await self._current_task
        return True

    async def retry_failed(self, job_id: str) -> tuple[str | None, int]:
        job_dir = self.jobstore.get(job_id)
        if job_dir is None:
            return None, 0

        retryable: set[str] = set()
        for row in job_dir.read_downloads() + job_dir.read_results():
            if row.get("ok"):
                continue
            raw = row.get("error_class")
            if not raw:
                continue
            try:
                if ErrorClass(raw).retryable:
                    retryable.add(str(row.get("post_id")))
            except ValueError:
                continue

        if not retryable:
            return None, 0

        # Un-mark them so the normal claim path picks them up again.
        await asyncio.to_thread(self._unmark_posts, retryable)
        new_job_id = await self.start_batch(
            limit=len(retryable), confirm_expired=True,
            mode="minio" if self.minio_available and self.active_upload_id is None else "upload",
        )
        return new_job_id, len(retryable)

    def _unmark_posts(self, post_ids: set[str]) -> None:
        self.checkpoint.remove(post_ids)
        if self.minio_source is not None:
            cfg = self.settings.minio
            storage = self.minio_source.storage
            try:
                rows = [
                    row for row in storage.iter_jsonl(cfg.processed_ids_key)
                    if str(row.get("post_id")) not in post_ids
                ]
                storage.put_jsonl(cfg.processed_ids_key, rows)
                self.minio_source.invalidate_cache()
            except Exception as exc:  # noqa: BLE001 - local checkpoint is authoritative
                log.warning("could not un-mark posts in MinIO: %s", exc)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def upload_stats(self, upload_id: str | None = None) -> dict[str, Any]:
        upload = self.uploads.get(upload_id) if upload_id else self.uploads.latest()
        if upload is None:
            return {"present": False}
        source = FileRecordSource(upload.path, self.checkpoint, self.settings)
        return {"present": True, **upload.to_json(), **source.stats()}

    async def pipeline_status(self) -> dict[str, Any]:
        """Corpus-level progress. Cached briefly — the UI polls every couple of
        seconds and this touches disk (and possibly MinIO)."""
        if time.time() - self._pipeline_cached_at < 10 and self._pipeline_cache:
            return self._pipeline_cache

        counts = await asyncio.to_thread(self.file_sink.counts)
        processed = await asyncio.to_thread(self.checkpoint.count)
        upload = await asyncio.to_thread(self.upload_stats)

        corpus_total = upload.get("scannable_posts", 0) if upload.get("present") else 0
        remaining = upload.get("pending_posts", 0) if upload.get("present") else 0

        minio_state: dict[str, Any] = {"configured": self.minio_available}
        if self.minio_available:
            try:
                runs = await asyncio.to_thread(self.minio_source.list_run_ids)
                minio_state.update({"connected": True, "crawl_runs_total": len(runs)})
            except Exception as exc:  # noqa: BLE001 - MinIO down must not 500 the UI
                minio_state.update({"connected": False, "error": f"{type(exc).__name__}: {exc}"})

        active = self.jobstore.active()
        payload = {
            "mode": "minio" if self.minio_available else "upload",
            "minio": minio_state,
            "upload": upload,
            "corpus_total": corpus_total,
            "processed_total": processed,
            "remaining": remaining,
            "percent": round(processed / corpus_total * 100, 1) if corpus_total else 0.0,
            "prepared": counts.get("ready_for_ocr", 0),
            "failed": counts["failed"],
            "downloads": self.file_sink.downloadable(),
            "active_job": active.to_json() if active else None,
            "busy": self.busy,
        }
        self._pipeline_cache = payload
        self._pipeline_cached_at = time.time()
        return payload
