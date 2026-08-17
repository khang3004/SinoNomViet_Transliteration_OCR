"""Keeps the scanner in step with the crawl.

Scanning starts right after crawling, so this stage runs continuously in small
batches rather than as one enormous job. Small batches matter for three reasons:
failures cost minutes instead of hours, progress is always visible, and images
are fetched while their signed URLs are still fresh.

A plain asyncio loop rather than APScheduler — the service runs a single uvicorn
worker (job state requires it), so there is nothing a scheduler library would add
here beyond a dependency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class BatchScheduler:
    def __init__(
        self,
        interval_s: int,
        run_batch: Callable[[], Awaitable[bool]],
        enabled: bool = True,
    ) -> None:
        """``run_batch`` returns True when it did work, False when idle."""
        self.interval_s = max(10, interval_s)
        self.run_batch = run_batch
        self.enabled = enabled
        self.paused = not enabled
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.last_run_at: float | None = None
        self.last_error: str = ""
        self.consecutive_errors = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="batch-scheduler")
        log.info(
            "scheduler started (interval=%ss, paused=%s)", self.interval_s, self.paused
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self._wake.set()  # do not make the operator wait a full interval

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.paused:
                try:
                    did_work = await self.run_batch()
                    self.last_error = ""
                    self.consecutive_errors = 0
                    if did_work:
                        # More work is likely queued; come back promptly rather
                        # than idling a full interval behind the crawler.
                        await self._sleep(min(15, self.interval_s))
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a bad tick must not end the loop
                    self.consecutive_errors += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("scheduled batch failed")

            await self._sleep(self._backoff_interval())

    def _backoff_interval(self) -> float:
        """Back off after repeated failures so a broken MinIO link does not
        produce a failed batch every interval for hours."""
        if self.consecutive_errors == 0:
            return self.interval_s
        return min(self.interval_s * (2 ** min(self.consecutive_errors, 4)), 3600)

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep — resume() and stop() take effect immediately."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "paused": self.paused,
            "running": self.running,
            "interval_s": self.interval_s,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
        }
