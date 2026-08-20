"""Batch orchestration: preflight -> download -> publish.

Preflight gates on URL expiry before spending any bandwidth: fbcdn signatures
last hours, so a stale crawl is worth re-running rather than downloading dead
links. Everything after that is network-bound and finishes in minutes.

Fault tolerance is the other design axis: a restart or a cancel must cost one
chunk, never the corpus. Results are published and checkpointed as they finish
rather than only at the end.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.config import Settings
from app.core.downloader import DownloadResult, ImageDownloader
from app.core.jobstore import JobDir, JobState, Phase
from app.core.models import (
    ErrorClass,
    PostObject,
    PreparedImage,
    PreparedPost,
    PrepError,
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
    ) -> JobState:
        """Run one batch: claim work, download images, publish signed URLs."""
        limit = limit or state.limit or self.settings.batch_size
        try:
            posts = await self._phase_preflight(job_dir, state, limit, confirm_expired)
            if posts is None:
                return state  # gated on confirmation, or nothing to do

            published: set[str] = set()
            await self._phase_download(
                job_dir, state, posts,
                # Publish finished posts as their images land, so stopping the
                # run keeps everything already completed.
                on_progress=lambda downloads: self._phase_publish(
                    job_dir, state, posts, downloads, published=published
                ),
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
        self,
        job_dir: JobDir,
        state: JobState,
        posts: list[PostObject],
        on_progress: Callable[[dict[str, DownloadResult]], Awaitable[None]] | None = None,
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
                # Flush finished posts so a stop keeps them.
                if on_progress is not None:
                    await on_progress(results)

        await self.downloader.download_all(
            pending, on_result=on_result, should_cancel=lambda: state.cancel_requested
        )

        # Fold in prior-run outcomes so publishing sees the full picture.
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
        if on_progress is not None:
            await on_progress(results)
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
    # Phase 3 - publish
    # ------------------------------------------------------------------

    @staticmethod
    def _post_is_resolved(
        post: PostObject, downloads: dict[str, DownloadResult]
    ) -> bool:
        """Every image has reached a terminal state (downloaded or failed).

        Only fully-resolved posts are published, so a record never describes a
        post whose remaining images are still in flight.
        """
        return all(
            f"{post.post_id}:{idx}" in downloads
            for idx in range(len(post.image_urls))
        )

    async def _phase_publish(
        self,
        job_dir: JobDir,
        state: JobState,
        posts: list[PostObject],
        downloads: dict[str, DownloadResult],
        published: set[str] | None = None,
    ) -> None:
        """Publish resolved posts and checkpoint them.

        Called repeatedly during the download phase, not just at the end, so a
        stop costs the in-flight items rather than the whole batch.
        ``published`` tracks what earlier calls already emitted.
        """
        published = published if published is not None else set()

        source_key = state.source_keys[0] if state.source_keys else ""
        source_run = state.source_run_ids[0] if state.source_run_ids else ""

        pending = [
            p for p in posts
            if p.post_id not in published and self._post_is_resolved(p, downloads)
        ]
        if not pending:
            return

        state.phase = Phase.PUBLISH
        records: list[PreparedPost] = []
        errors: list[PrepError] = []

        for post in pending:
            images: list[PreparedImage] = []
            failed = 0

            for idx, url in enumerate(post.image_urls):
                download = downloads.get(f"{post.post_id}:{idx}")

                if download is None or not download.ok:
                    failed += 1
                    errors.append(
                        PrepError(
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
                            run_id=state.scan_run_id,
                        )
                    )
                    continue

                suffix = Path(download.local_path).suffix or ".jpg"
                signed_url, expires_at = self.signer.build(post.post_id, idx, suffix)
                images.append(
                    PreparedImage(
                        url=signed_url, idx=idx,
                        width=download.width, height=download.height,
                        bytes=download.bytes_len, content_type=download.content_type,
                        sha256=download.sha256, source_url=url,
                        source_expires_at=download.item.source_expires_at,
                        url_expires_at=datetime.fromtimestamp(
                            expires_at, tz=timezone.utc
                        ).isoformat(),
                        downloaded_at=download.downloaded_at,
                    )
                )

            if not images:
                # Every image failed — errors only, nothing to hand downstream.
                continue

            records.append(
                PreparedPost(
                    post_id=post.post_id, group_id=post.group_id,
                    post_link=post.post_link, author=post.author,
                    story_post_id=post.story_post_id, tile_id=post.tile_id,
                    images_prepared=len(images), images_failed=failed, images=images,
                    source_key=source_key, source_run_id=source_run,
                    run_id=state.scan_run_id, prepared_at=utcnow_iso(),
                    label=post.label, sub_caption=post.sub_caption,
                    posted_at=post.posted_at,
                )
            )

        # += not =, since this runs once per flush.
        state.counts.prepared += len(records)
        state.counts.published += len(records)
        published.update(r.post_id for r in records)

        await asyncio.to_thread(self.sink.write_results, records, state.scan_run_id)
        await asyncio.to_thread(self.sink.write_errors, errors, state.scan_run_id)

        # Only mark posts done once their records are durably written — marking
        # earlier would silently drop posts if publishing failed.
        await asyncio.to_thread(
            self.source.mark_done, [r.post_id for r in records], state.scan_run_id
        )

        summary = {
            "job_id": state.job_id,
            "run_id": state.scan_run_id,
            "counts": state.counts.__dict__,
            "preflight": state.preflight,
            "source_keys": state.source_keys,
            "source_run_ids": state.source_run_ids,
            "cancelled": state.cancel_requested,
        }
        await asyncio.to_thread(self.sink.write_run_summary, summary, state.scan_run_id)

        job_dir.append_event(
            "info",
            f"published {len(records)} post(s), {len(errors)} image error(s)",
        )
        state.phase = Phase.DOWNLOAD
        job_dir.save_state(state)
