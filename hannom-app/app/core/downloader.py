"""Fetching images from the Facebook CDN.

This phase runs to completion *before* OCR starts. That ordering is forced by
expiry: fbcdn signs URLs with a lifetime measured in hours, while OCR of a full
corpus takes hours. Interleaving them would mean the last image is fetched long
after its signature died. Downloading first, fast and at high concurrency, is
what keeps the pipeline correct.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.core.config import DownloadConfig
from app.core.models import ErrorClass, WorkItem, utcnow_iso
from app.core.parser import is_expired

log = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    item: WorkItem
    ok: bool
    local_path: str | None = None
    bytes_len: int | None = None
    content_type: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    downloaded_at: str = ""
    attempts: int = 0
    error_class: ErrorClass | None = None
    error_detail: str = ""
    http_status: int | None = None


def classify_http_status(status: int) -> ErrorClass:
    if status == 403:
        # fbcdn returns 403 for signature failures — usually an expired URL.
        return ErrorClass.HTTP_403
    if status == 404:
        return ErrorClass.HTTP_404
    if status == 429:
        return ErrorClass.HTTP_429
    if status >= 500:
        return ErrorClass.HTTP_5XX
    return ErrorClass.HTTP_404


def classify_exception(exc: BaseException) -> tuple[ErrorClass, str]:
    """Map a transport exception to an error class.

    Imports httpx lazily so this module stays importable (and unit-testable)
    without the dependency present.
    """
    detail = f"{type(exc).__name__}: {exc}"
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ErrorClass.TIMEOUT, detail
        if isinstance(exc, httpx.ConnectError):
            lowered = str(exc).lower()
            if "name" in lowered or "resolve" in lowered or "dns" in lowered:
                return ErrorClass.DNS_ERROR, detail
            return ErrorClass.HTTP_5XX, detail
        if isinstance(exc, httpx.HTTPError):
            return ErrorClass.HTTP_5XX, detail
    except ModuleNotFoundError:
        pass

    if isinstance(exc, asyncio.TimeoutError):
        return ErrorClass.TIMEOUT, detail
    return ErrorClass.HTTP_5XX, detail


class AdaptiveLimiter:
    """Concurrency that backs off when the CDN pushes back.

    Sustained 429s halve the ceiling (down to a floor); a clean streak lets it
    climb back. At 500 images per batch this should never engage — it exists so
    that a rate-limit response degrades throughput instead of the run.
    """

    def __init__(self, initial: int, minimum: int) -> None:
        self.limit = max(1, initial)
        self.initial = max(1, initial)
        self.minimum = max(1, min(minimum, initial))
        self._sem = asyncio.Semaphore(self.limit)
        self._throttle_hits = 0
        self._clean_streak = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._sem.release()

    async def record_throttled(self) -> None:
        async with self._lock:
            self._throttle_hits += 1
            self._clean_streak = 0
            # Three strikes before reacting, so one stray 429 is not a signal.
            if self._throttle_hits >= 3 and self.limit > self.minimum:
                self.limit = max(self.minimum, self.limit // 2)
                self._throttle_hits = 0
                log.warning("CDN throttling detected; concurrency -> %d", self.limit)

    async def record_ok(self) -> None:
        async with self._lock:
            self._clean_streak += 1
            if self._clean_streak >= 200 and self.limit < self.initial:
                self.limit = min(self.initial, self.limit + 1)
                self._clean_streak = 0
                log.info("CDN recovered; concurrency -> %d", self.limit)


def _backoff_delay(attempt: int, cap: float = 30.0) -> float:
    """Exponential backoff with jitter, capped.

    Jitter matters: without it, a batch that hits a 429 retries in lockstep and
    trips the limit again. The cap is applied *after* jitter — capping first
    would let the 0.5-1.5x multiplier push the delay past the ceiling.
    """
    return min(cap, (2 ** attempt) * 0.5 * (0.5 + random.random()))


class ImageDownloader:
    def __init__(self, cfg: DownloadConfig, images_dir: Path) -> None:
        self.cfg = cfg
        self.images_dir = images_dir

    def local_path_for(self, post_id: str, idx: int, suffix: str = ".jpg") -> Path:
        # Shard by the post id prefix: 20k files in one directory is slow to
        # stat on most filesystems.
        shard = (post_id[:2] or "00").lower()
        return self.images_dir / shard / f"{post_id}_{idx}{suffix}"

    def existing_file(self, post_id: str, idx: int) -> Path | None:
        """An already-downloaded image for this (post, index), if any.

        Filenames are deterministic, so a non-empty match is the same image.
        This is what makes an interrupted run cheap to redo: a new batch reuses
        what is on disk instead of re-fetching from the CDN — which also matters
        because those signed URLs may have expired in the meantime.
        """
        shard = (post_id[:2] or "00").lower()
        for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            candidate = self.images_dir / shard / f"{post_id}_{idx}{suffix}"
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                continue
        return None

    def _from_disk(self, item: WorkItem, path: Path) -> DownloadResult:
        """Rebuild a result from a file already on disk, without a network call."""
        from app.core.ocr import image_dimensions

        data = path.read_bytes()
        width, height = image_dimensions(data)
        content_type = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(path.suffix.lower())
        return DownloadResult(
            item=item, ok=True, local_path=str(path), bytes_len=len(data),
            content_type=content_type, sha256=hashlib.sha256(data).hexdigest(),
            width=width, height=height, downloaded_at=utcnow_iso(), attempts=0,
        )

    async def _fetch_one(
        self,
        client,
        item: WorkItem,
        limiter: AdaptiveLimiter,
    ) -> DownloadResult:
        # Reuse a previous run's file before considering the URL at all — it may
        # have expired since, and re-fetching bytes we already hold is waste.
        existing = self.existing_file(item.post_id, item.idx)
        if existing is not None:
            try:
                return self._from_disk(item, existing)
            except OSError as exc:
                log.warning("could not reuse %s (%s); re-downloading", existing, exc)

        # Do not spend a request on a URL we can prove is dead.
        if is_expired(item.source_url):
            return DownloadResult(
                item=item, ok=False, attempts=0,
                error_class=ErrorClass.EXPIRED_URL,
                error_detail=f"signed URL expired at {item.source_expires_at}",
            )

        last_class = ErrorClass.HTTP_5XX
        last_detail = ""
        last_status: int | None = None

        for attempt in range(1, self.cfg.max_attempts + 1):
            try:
                async with limiter:
                    response = await client.get(item.source_url)

                status = response.status_code
                if status == 200:
                    data = response.content
                    if len(data) > self.cfg.max_bytes:
                        return DownloadResult(
                            item=item, ok=False, attempts=attempt,
                            error_class=ErrorClass.TOO_LARGE,
                            error_detail=f"{len(data)} bytes exceeds cap",
                            http_status=status,
                        )
                    await limiter.record_ok()
                    return self._persist(item, data, response.headers, attempt)

                last_status = status
                last_class = classify_http_status(status)
                last_detail = f"HTTP {status}"

                if last_class is ErrorClass.HTTP_429:
                    await limiter.record_throttled()
                if not last_class.retryable:
                    break

            except Exception as exc:  # noqa: BLE001 - classified, then retried
                last_class, last_detail = classify_exception(exc)
                if not last_class.retryable:
                    break

            if attempt < self.cfg.max_attempts:
                await asyncio.sleep(_backoff_delay(attempt))

        return DownloadResult(
            item=item, ok=False, attempts=self.cfg.max_attempts,
            error_class=last_class, error_detail=last_detail, http_status=last_status,
        )

    def _persist(self, item: WorkItem, data: bytes, headers, attempt: int) -> DownloadResult:
        from app.core.ocr import image_dimensions

        content_type = (headers.get("content-type") or "").split(";")[0].strip()
        suffix = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
            "image/webp": ".webp", "image/gif": ".gif",
        }.get(content_type, ".jpg")

        path = self.local_path_for(item.post_id, item.idx, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp name then rename: a crash mid-write must not leave a
        # truncated file that a resume would happily treat as complete.
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

        width, height = image_dimensions(data)
        return DownloadResult(
            item=item, ok=True, local_path=str(path), bytes_len=len(data),
            content_type=content_type or None,
            sha256=hashlib.sha256(data).hexdigest(),
            width=width, height=height,
            downloaded_at=utcnow_iso(), attempts=attempt,
        )

    async def download_all(
        self,
        items: list[WorkItem],
        on_result: Callable[[DownloadResult], Awaitable[None]] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[DownloadResult]:
        """Fetch every item, reporting each result as it lands.

        Results are streamed to ``on_result`` rather than only returned, so the
        caller can persist progress incrementally and survive a crash.
        """
        if not items:
            return []

        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency 'httpx'.") from exc

        limiter = AdaptiveLimiter(self.cfg.concurrency, self.cfg.min_concurrency)
        results: list[DownloadResult] = []

        timeout = httpx.Timeout(self.cfg.timeout_s)
        limits = httpx.Limits(max_connections=self.cfg.concurrency * 2)
        headers = {"User-Agent": self.cfg.user_agent}

        async with httpx.AsyncClient(
            timeout=timeout, limits=limits, headers=headers, follow_redirects=True
        ) as client:
            pending = [
                asyncio.create_task(self._fetch_one(client, item, limiter))
                for item in items
            ]
            try:
                for coro in asyncio.as_completed(pending):
                    result = await coro
                    results.append(result)
                    if on_result is not None:
                        await on_result(result)
                    if should_cancel is not None and should_cancel():
                        log.info("download cancelled after %d items", len(results))
                        break
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()

        return results
