"""Batch orchestration: preflight -> download -> ocr -> publish.

Phase order is forced by URL expiry. fbcdn signatures last hours; OCR of a full
corpus takes hours. Downloading everything first (minutes, network-bound) and
only then scanning (hours, CPU-bound) is what keeps the last image as valid as
the first.

Fault tolerance is the other design axis: a poison image, an OOM, or a container
restart must each cost one item or one batch, never the corpus.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core import ocr as ocr_module
from app.core.config import Settings
from app.core.downloader import DownloadResult, ImageDownloader
from app.core.jobstore import JobDir, JobState, Phase
from app.core.models import (
    SCAN_SCANNED,
    SCAN_SKIPPED,
    ErrorClass,
    HanScanError,
    HanScanRecord,
    PostObject,
    ScannedImage,
    ScanOutcome,
    WorkItem,
    utcnow_iso,
)
from app.core.parser import preflight_expiry, to_work_items
from app.core.signing import ImageUrlSigner
from app.core.sink import ResultSink
from app.core.source import RecordSource

log = logging.getLogger(__name__)

# Expired-URL share above which the run pauses for a human decision. A fresh
# crawl should be ~0%; anything meaningful means the crawler and scanner have
# drifted and re-crawling beats scanning dead links for hours.
EXPIRY_GATE_RATIO = 0.10


def process_rss_mb(pids: list[int]) -> float:
    """Total RSS of the given processes, in MB. 0.0 if psutil is unavailable."""
    try:
        import psutil
    except ModuleNotFoundError:
        return 0.0
    total = 0.0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_info().rss / (1024 * 1024)
        except Exception:  # noqa: BLE001 - worker already gone
            continue
    return total


class BatchRunner:
    """Runs one batch to completion, persisting every terminal state as it goes."""

    def __init__(
        self,
        settings: Settings,
        source: RecordSource,
        sink: ResultSink,
        signer: ImageUrlSigner | None = None,
    ) -> None:
        self.settings = settings
        self.source = source
        self.sink = sink
        self.signer = signer or ImageUrlSigner(
            base_url=settings.images.public_base_url,
            secret=settings.images.signing_secret,
            ttl_days=settings.images.ttl_days,
        )
        self.downloader = ImageDownloader(settings.download, settings.images_dir)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        job_dir: JobDir,
        state: JobState,
        *,
        confirm_expired: bool = False,
        limit: int | None = None,
        run_ocr: bool = True,
    ) -> JobState:
        """Run one batch.

        ``run_ocr=False`` stops after downloading: images are fetched, stored and
        given signed URLs, but never scanned. That is the cheap mode — OCR is
        what saturates the CPU, and for a pipeline whose next stage runs Gemini
        on these images anyway, scanning here can be redundant.
        """
        limit = limit or state.limit or self.settings.batch_size
        state.run_ocr = run_ocr
        try:
            posts = await self._phase_preflight(job_dir, state, limit, confirm_expired)
            if posts is None:
                return state  # gated on confirmation, or nothing to do

            downloads = await self._phase_download(job_dir, state, posts)
            if run_ocr:
                outcomes = await self._phase_ocr(job_dir, state, downloads)
            else:
                outcomes = {}
                job_dir.append_event(
                    "info", "OCR skipped — producing signed image URLs only"
                )
            await self._phase_publish(
                job_dir, state, posts, downloads, outcomes, run_ocr=run_ocr
            )

            state.phase = Phase.CANCELLED if state.cancel_requested else Phase.DONE
        except Exception as exc:  # noqa: BLE001 - a failed batch must not kill the service
            log.exception("batch %s failed", state.job_id)
            state.phase = Phase.FAILED
            state.error = f"{type(exc).__name__}: {exc}"
            job_dir.append_event("error", f"batch failed: {state.error}")
        finally:
            job_dir.save_state(state)

        job_dir.append_event("info", f"batch finished in phase {state.phase.value}")
        return state

    # ------------------------------------------------------------------
    # Phase 0 - preflight
    # ------------------------------------------------------------------

    async def _phase_preflight(
        self, job_dir: JobDir, state: JobState, limit: int, confirm_expired: bool
    ) -> list[PostObject] | None:
        state.phase = Phase.PREFLIGHT
        job_dir.save_state(state)
        job_dir.append_event("info", "preflight: claiming work")

        batch = await asyncio.to_thread(self.source.iter_pending, limit)

        state.source_keys = batch.source_keys
        state.source_run_ids = batch.source_run_ids
        state.corpus_total = batch.corpus_total
        state.processed_total = batch.processed_total
        state.runs_total = batch.runs_total
        state.runs_done = batch.runs_done
        state.counts.total_posts = len(batch.posts)

        if batch.malformed_lines:
            job_dir.append_event(
                "warn", f"{batch.malformed_lines} malformed line(s) skipped while parsing"
            )

        if not batch.posts:
            job_dir.append_event("info", "no pending posts — nothing to do")
            state.phase = Phase.DONE
            job_dir.save_state(state)
            return None

        report = preflight_expiry(batch.posts, warn_within=timedelta(hours=6))
        state.preflight = report
        state.counts.total_images = report["total"]
        job_dir.save_state(state)
        job_dir.append_event(
            "info",
            "preflight: {total} images — {expired} expired, {expiring_soon} expiring soon, "
            "{healthy} healthy, {unknown_expiry} unknown".format(**report),
        )

        expired_ratio = report["expired"] / report["total"] if report["total"] else 0.0
        if expired_ratio > EXPIRY_GATE_RATIO and not confirm_expired:
            state.awaiting_confirmation = True
            state.phase = Phase.PENDING
            job_dir.save_state(state)
            job_dir.append_event(
                "warn",
                f"{expired_ratio:.0%} of URLs already expired — paused for confirmation. "
                "Re-crawling is usually better than scanning dead links.",
            )
            return None

        state.awaiting_confirmation = False

        # The manifest is written once and never mutated — it is the immutable
        # work list that resume replays against.
        items: list[WorkItem] = []
        for post in batch.posts:
            items.extend(to_work_items(post))
        job_dir.write_manifest(
            [
                {
                    "post_id": i.post_id,
                    "idx": i.idx,
                    "source_url": i.source_url,
                    "source_expires_at": i.source_expires_at,
                }
                for i in items
            ]
        )
        return batch.posts

    # ------------------------------------------------------------------
    # Phase 1 - download
    # ------------------------------------------------------------------

    async def _phase_download(
        self, job_dir: JobDir, state: JobState, posts: list[PostObject]
    ) -> dict[str, DownloadResult]:
        state.phase = Phase.DOWNLOAD
        job_dir.save_state(state)

        manifest = job_dir.read_manifest()
        already = job_dir.completed_download_keys()

        pending = [
            WorkItem(
                post_id=m["post_id"], idx=m["idx"], source_url=m["source_url"],
                source_expires_at=m.get("source_expires_at"),
            )
            for m in manifest
            if f"{m['post_id']}:{m['idx']}" not in already
        ]

        if already:
            job_dir.append_event(
                "info", f"resuming: {len(already)} image(s) already downloaded"
            )
        job_dir.append_event("info", f"downloading {len(pending)} image(s)")

        results: dict[str, DownloadResult] = {}
        counter = {"n": 0}

        async def on_result(result: DownloadResult) -> None:
            key = f"{result.item.post_id}:{result.item.idx}"
            results[key] = result
            job_dir.append_download(
                {
                    "post_id": result.item.post_id,
                    "idx": result.item.idx,
                    "ok": result.ok,
                    "local_path": result.local_path,
                    "bytes": result.bytes_len,
                    "content_type": result.content_type,
                    "sha256": result.sha256,
                    "width": result.width,
                    "height": result.height,
                    "downloaded_at": result.downloaded_at,
                    "attempts": result.attempts,
                    "http_status": result.http_status,
                    "error_class": result.error_class.value if result.error_class else None,
                    "error_detail": result.error_detail,
                    "source_url": result.item.source_url,
                    "source_expires_at": result.item.source_expires_at,
                }
            )
            if result.ok:
                state.counts.downloaded += 1
            else:
                state.counts.download_failed += 1

            counter["n"] += 1
            if counter["n"] % 25 == 0:
                job_dir.heartbeat(state)
                job_dir.append_event(
                    "info",
                    f"downloaded {state.counts.downloaded}, "
                    f"failed {state.counts.download_failed}",
                )

        await self.downloader.download_all(
            pending, on_result=on_result, should_cancel=lambda: state.cancel_requested
        )

        # Fold in prior-run outcomes so the OCR phase sees the full picture.
        for key, row in already.items():
            if key in results:
                continue
            results[key] = self._download_result_from_row(row)

        job_dir.heartbeat(state)
        job_dir.append_event(
            "info",
            f"download phase done: {state.counts.downloaded} ok, "
            f"{state.counts.download_failed} failed",
        )
        return results

    @staticmethod
    def _download_result_from_row(row: dict[str, Any]) -> DownloadResult:
        error_class = row.get("error_class")
        return DownloadResult(
            item=WorkItem(
                post_id=row.get("post_id", ""), idx=row.get("idx", 0),
                source_url=row.get("source_url", ""),
                source_expires_at=row.get("source_expires_at"),
            ),
            ok=bool(row.get("ok")),
            local_path=row.get("local_path"),
            bytes_len=row.get("bytes"),
            content_type=row.get("content_type"),
            sha256=row.get("sha256"),
            width=row.get("width"),
            height=row.get("height"),
            downloaded_at=row.get("downloaded_at", ""),
            attempts=row.get("attempts", 0),
            http_status=row.get("http_status"),
            error_class=ErrorClass(error_class) if error_class else None,
            error_detail=row.get("error_detail", ""),
        )

    # ------------------------------------------------------------------
    # Phase 2 - OCR
    # ------------------------------------------------------------------

    async def _phase_ocr(
        self, job_dir: JobDir, state: JobState, downloads: dict[str, DownloadResult]
    ) -> dict[str, ScanOutcome]:
        state.phase = Phase.OCR
        job_dir.save_state(state)

        already = job_dir.completed_scan_keys()
        todo = [
            result
            for key, result in downloads.items()
            if result.ok and result.local_path and key not in already
        ]

        outcomes: dict[str, ScanOutcome] = {
            key: ScanOutcome(
                idx=row.get("idx", 0), post_id=row.get("post_id", ""),
                ok=bool(row.get("ok")), valid_pic=bool(row.get("valid_pic")),
                han_words=row.get("han_words", 0), boxes=row.get("boxes", 0),
                texts=row.get("texts", []), mean_confidence=row.get("mean_confidence"),
                scan_ms=row.get("scan_ms", 0),
            )
            for key, row in already.items()
        }

        if already:
            job_dir.append_event("info", f"resuming: {len(already)} image(s) already scanned")
        if not todo:
            job_dir.append_event("info", "no images to scan")
            return outcomes

        workers = max(1, self.settings.ocr.workers)
        state.ocr_workers = workers
        job_dir.save_state(state)
        job_dir.append_event("info", f"scanning {len(todo)} image(s) with {workers} worker(s)")

        loop = asyncio.get_running_loop()
        pool = self._new_pool(workers)
        chunk_size = max(1, workers * 4)

        try:
            for start in range(0, len(todo), chunk_size):
                if state.cancel_requested:
                    job_dir.append_event("warn", "cancel requested — stopping OCR")
                    break

                chunk = todo[start : start + chunk_size]
                try:
                    chunk_outcomes = await self._run_chunk(loop, pool, chunk)
                except BrokenProcessPool:
                    # A worker died mid-chunk, which kills every pending future.
                    # Rebuild and retry the chunk serially so the poison image is
                    # isolated to itself instead of taking the batch down.
                    job_dir.append_event(
                        "warn", "OCR worker died — rebuilding pool and isolating the chunk"
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = self._new_pool(workers)
                    chunk_outcomes, pool = await self._run_chunk_serially(
                        loop, pool, chunk, job_dir, workers
                    )

                for key, outcome in chunk_outcomes.items():
                    outcomes[key] = outcome
                    job_dir.append_result(
                        {
                            "post_id": outcome.post_id, "idx": outcome.idx,
                            "ok": outcome.ok, "valid_pic": outcome.valid_pic,
                            "han_words": outcome.han_words, "boxes": outcome.boxes,
                            "texts": outcome.texts,
                            "mean_confidence": outcome.mean_confidence,
                            "scan_ms": outcome.scan_ms,
                            "error_class": outcome.error_class.value if outcome.error_class else None,
                            "error_detail": outcome.error_detail,
                        }
                    )
                    if outcome.ok:
                        state.counts.scanned += 1
                    else:
                        state.counts.scan_failed += 1

                workers = self._enforce_memory_guard(job_dir, pool, workers)
                job_dir.heartbeat(state)
                job_dir.append_event(
                    "info",
                    f"scanned {state.counts.scanned}/{len(todo)}, "
                    f"failed {state.counts.scan_failed}",
                )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        return outcomes

    def _new_pool(self, workers: int) -> ProcessPoolExecutor:
        """Pool whose workers each load their own model at construction.

        A per-process engine is the point: PaddleOCR is not safe to share across
        threads, and paying the load cost in the initializer keeps it off the
        first image's latency.
        """
        return ProcessPoolExecutor(
            max_workers=workers,
            initializer=ocr_module.pool_initializer,
            initargs=(self.settings.ocr.lang, self.settings.ocr.enable_mkldnn),
        )

    async def _run_chunk(
        self, loop, pool: ProcessPoolExecutor, chunk: list[DownloadResult]
    ) -> dict[str, ScanOutcome]:
        tasks = {
            f"{r.item.post_id}:{r.item.idx}": loop.run_in_executor(
                pool, self._scan_call, r
            )
            for r in chunk
        }
        out: dict[str, ScanOutcome] = {}
        try:
            for key, future in tasks.items():
                post_id, idx = key.split(":")
                try:
                    out[key] = await asyncio.wait_for(future, self.settings.ocr.timeout_s)
                except asyncio.TimeoutError:
                    # One pathological image must not stall a worker forever.
                    out[key] = ScanOutcome(
                        idx=int(idx), post_id=post_id, ok=False,
                        error_class=ErrorClass.OCR_ERROR, error_detail="OCR timed out",
                    )
                except ocr_module.OcrUnavailable:
                    # The engine itself is broken (bad image build, missing model).
                    # Let this kill the batch: marking 20k images "ocr_error" and
                    # flagging their posts done would quietly destroy the corpus.
                    raise
                except BrokenProcessPool:
                    raise  # handled by the caller, which rebuilds and isolates
                except Exception as exc:  # noqa: BLE001 - one image, never the batch
                    out[key] = ScanOutcome(
                        idx=int(idx), post_id=post_id, ok=False,
                        error_class=ErrorClass.OCR_ERROR,
                        error_detail=f"{type(exc).__name__}: {exc}",
                    )
        finally:
            # A broken pool fails every pending future at once. Leaving them
            # un-awaited makes asyncio dump a traceback per future at GC time,
            # which would bury real errors in a multi-hour run's log.
            await self._drain(tasks, out)
        return out

    @staticmethod
    async def _drain(tasks: dict[str, Any], collected: dict[str, ScanOutcome]) -> None:
        for key, future in tasks.items():
            if key in collected or not hasattr(future, "done"):
                continue
            if not future.done():
                future.cancel()
            try:
                await future
            except BaseException:  # noqa: BLE001 - retrieval only, already recorded
                pass

    async def _run_chunk_serially(
        self,
        loop,
        pool: ProcessPoolExecutor,
        chunk: list[DownloadResult],
        job_dir: JobDir,
        workers: int,
    ) -> tuple[dict[str, ScanOutcome], ProcessPoolExecutor]:
        """One at a time, so a crashing image is attributed to itself.

        Returns the (possibly rebuilt) pool: a poison image kills the pool it
        runs in, and without rebuilding, every later image in the chunk would be
        blamed for a failure that already happened.
        """
        out: dict[str, ScanOutcome] = {}
        for result in chunk:
            key = f"{result.item.post_id}:{result.item.idx}"
            try:
                future = loop.run_in_executor(pool, self._scan_call, result)
                out[key] = await asyncio.wait_for(future, self.settings.ocr.timeout_s)
            except ocr_module.OcrUnavailable:
                raise  # engine-level failure, not an image-level one
            except Exception as exc:  # noqa: BLE001 - attribute it and move on
                job_dir.append_event(
                    "error", f"image {key} failed in isolation: {type(exc).__name__}"
                )
                out[key] = ScanOutcome(
                    idx=result.item.idx, post_id=result.item.post_id, ok=False,
                    error_class=ErrorClass.OCR_ERROR,
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
                if isinstance(exc, BrokenProcessPool):
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = self._new_pool(workers)
        return out, pool

    def _scan_call(self, result: DownloadResult) -> ScanOutcome:
        return ocr_module.scan_file(
            result.local_path,
            result.item.post_id,
            result.item.idx,
            lang=self.settings.ocr.lang,
            min_confidence=self.settings.ocr.min_confidence,
            enable_mkldnn=self.settings.ocr.enable_mkldnn,
        )

    def _enforce_memory_guard(
        self, job_dir: JobDir, pool: ProcessPoolExecutor, workers: int
    ) -> int:
        """Shrink the pool before the OOM killer does it for us.

        On an 8 GB box with ~1 GB per PaddleOCR worker, this is the difference
        between a slow batch and a batch that dies at hour four.
        """
        limit = self.settings.ocr.memory_limit_mb
        if limit <= 0 or workers <= 1:
            return workers

        pids = [p.pid for p in getattr(pool, "_processes", {}).values()]
        if not pids:
            return workers

        used = process_rss_mb(pids)
        if used > limit:
            job_dir.append_event(
                "warn",
                f"OCR workers using {used:.0f} MB (limit {limit} MB) — "
                f"reducing to {workers - 1} worker(s) on the next pool rebuild",
            )
            return workers - 1
        return workers

    # ------------------------------------------------------------------
    # Phase 3 - publish
    # ------------------------------------------------------------------

    async def _phase_publish(
        self,
        job_dir: JobDir,
        state: JobState,
        posts: list[PostObject],
        downloads: dict[str, DownloadResult],
        outcomes: dict[str, ScanOutcome],
        run_ocr: bool = True,
    ) -> None:
        state.phase = Phase.PUBLISH
        job_dir.save_state(state)

        # Don't claim an engine produced a verdict when none ran.
        engine = ocr_module.engine_label(self.settings.ocr.engine_name) if run_ocr else ""
        source_key = state.source_keys[0] if state.source_keys else ""
        source_run = state.source_run_ids[0] if state.source_run_ids else ""

        records: list[HanScanRecord] = []
        errors: list[HanScanError] = []

        for post in posts:
            images: list[ScannedImage] = []
            failed = 0

            for idx, url in enumerate(post.image_urls):
                key = f"{post.post_id}:{idx}"
                download = downloads.get(key)
                outcome = outcomes.get(key)

                if download is None or not download.ok:
                    failed += 1
                    errors.append(
                        HanScanError(
                            post_id=post.post_id, group_id=post.group_id,
                            source_url=url, idx=idx,
                            source_expires_at=(
                                download.item.source_expires_at if download else None
                            ),
                            error_class=(
                                download.error_class if download and download.error_class
                                else ErrorClass.TIMEOUT
                            ),
                            error_detail=download.error_detail if download else "not attempted",
                            http_status=download.http_status if download else None,
                            attempts=download.attempts if download else 0,
                            scan_run_id=state.scan_run_id,
                        )
                    )
                    continue

                # Only treat a missing outcome as a failure when OCR was meant to
                # run. In skip mode there is nothing to be missing.
                if run_ocr and (outcome is None or not outcome.ok):
                    failed += 1
                    errors.append(
                        HanScanError(
                            post_id=post.post_id, group_id=post.group_id,
                            source_url=url, idx=idx,
                            source_expires_at=download.item.source_expires_at,
                            error_class=(
                                outcome.error_class if outcome and outcome.error_class
                                else ErrorClass.OCR_ERROR
                            ),
                            error_detail=outcome.error_detail if outcome else "not scanned",
                            attempts=download.attempts,
                            scan_run_id=state.scan_run_id,
                        )
                    )
                    continue

                suffix = Path(download.local_path).suffix or ".jpg"
                signed_url, expires_at = self.signer.build(post.post_id, idx, suffix)
                images.append(
                    ScannedImage(
                        url=signed_url, idx=idx,
                        width=download.width, height=download.height,
                        bytes=download.bytes_len, content_type=download.content_type,
                        sha256=download.sha256, source_url=url,
                        source_expires_at=download.item.source_expires_at,
                        url_expires_at=datetime.fromtimestamp(
                            expires_at, tz=timezone.utc
                        ).isoformat(),
                        downloaded_at=download.downloaded_at,
                        # None, not False — nothing examined this image.
                        valid_pic=outcome.valid_pic if outcome else None,
                        han_words=outcome.han_words if outcome else None,
                        boxes=outcome.boxes if outcome else None,
                        texts=outcome.texts if outcome else [],
                        mean_confidence=outcome.mean_confidence if outcome else None,
                        scan_ms=outcome.scan_ms if outcome else None,
                    )
                )

            if not images and failed:
                # Every image failed — no verdict to publish, only errors.
                continue
            if not images:
                continue

            records.append(
                HanScanRecord(
                    post_id=post.post_id, group_id=post.group_id,
                    post_link=post.post_link, author=post.author,
                    story_post_id=post.story_post_id, tile_id=post.tile_id,
                    scan_status=SCAN_SCANNED if run_ocr else SCAN_SKIPPED,
                    han_valid=any(i.valid_pic for i in images) if run_ocr else None,
                    han_words_total=(
                        sum(i.han_words or 0 for i in images) if run_ocr else None
                    ),
                    images_scanned=len(images), images_failed=failed, images=images,
                    source_key=source_key, source_run_id=source_run,
                    scan_run_id=state.scan_run_id, ocr_engine=engine,
                    scanned_at=utcnow_iso(), label=post.label,
                    sub_caption=post.sub_caption, posted_at=post.posted_at,
                )
            )

        state.counts.han_valid = sum(1 for r in records if r.han_valid)
        state.counts.han_invalid = sum(
            1 for r in records if r.scan_status == SCAN_SCANNED and not r.han_valid
        )
        state.counts.ready_for_ocr = sum(
            1 for r in records if r.scan_status == SCAN_SKIPPED
        )
        state.counts.published = len(records)

        await asyncio.to_thread(self.sink.write_results, records, state.scan_run_id)
        await asyncio.to_thread(self.sink.write_errors, errors, state.scan_run_id)

        # Only mark posts done once their verdicts are durably in MinIO —
        # marking earlier would silently drop posts if publishing failed.
        await asyncio.to_thread(
            self.source.mark_done, [r.post_id for r in records], state.scan_run_id
        )

        summary = {
            "job_id": state.job_id,
            "scan_run_id": state.scan_run_id,
            "counts": state.counts.__dict__,
            "preflight": state.preflight,
            "source_keys": state.source_keys,
            "source_run_ids": state.source_run_ids,
            "ocr_engine": engine,
            "ocr_run": run_ocr,
            "cancelled": state.cancel_requested,
        }
        await asyncio.to_thread(self.sink.write_run_summary, summary, state.scan_run_id)

        if run_ocr:
            job_dir.append_event(
                "info",
                f"published {len(records)} record(s): {state.counts.han_valid} han_valid, "
                f"{state.counts.han_invalid} han_invalid, {len(errors)} error(s)",
            )
        else:
            job_dir.append_event(
                "info",
                f"published {len(records)} record(s) to ready_for_ocr.jsonl "
                f"(not scanned), {len(errors)} error(s)",
            )
