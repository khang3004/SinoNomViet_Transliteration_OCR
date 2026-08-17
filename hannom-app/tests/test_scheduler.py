"""The scheduler is what keeps the scanner in step with the crawl.

If it stalls or dies quietly, the pipeline stops without anything failing —
so the failure-backoff and pause/resume paths are worth pinning down.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.health import image_store
from app.core.scheduler import BatchScheduler


@pytest.fixture
async def stopped_scheduler():
    created: list[BatchScheduler] = []

    def make(**kwargs):
        scheduler = BatchScheduler(**kwargs)
        created.append(scheduler)
        return scheduler

    yield make
    for scheduler in created:
        await scheduler.stop()


class TestInterval:
    def test_interval_has_a_sane_floor(self):
        # A 1-second interval would hammer MinIO for no benefit.
        assert BatchScheduler(1, lambda: None).interval_s == 10

    def test_disabled_scheduler_starts_paused(self):
        assert BatchScheduler(300, lambda: None, enabled=False).paused is True


class TestBackoff:
    def test_healthy_scheduler_uses_the_plain_interval(self):
        scheduler = BatchScheduler(300, lambda: None)
        assert scheduler._backoff_interval() == 300

    def test_repeated_failures_back_off_exponentially(self):
        # A broken MinIO link should not produce a failed batch every interval
        # for hours on end.
        scheduler = BatchScheduler(60, lambda: None)
        delays = []
        for n in range(1, 5):
            scheduler.consecutive_errors = n
            delays.append(scheduler._backoff_interval())
        assert delays == sorted(delays) and delays[0] < delays[-1]

    def test_backoff_is_capped(self):
        scheduler = BatchScheduler(600, lambda: None)
        scheduler.consecutive_errors = 99
        assert scheduler._backoff_interval() <= 3600


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_runs_the_callback(self, stopped_scheduler):
        calls = []

        async def work():
            calls.append(1)
            return False

        scheduler = stopped_scheduler(interval_s=10, run_batch=work)
        scheduler.start()
        await asyncio.sleep(0.15)
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_paused_scheduler_does_no_work(self, stopped_scheduler):
        calls = []

        async def work():
            calls.append(1)
            return False

        scheduler = stopped_scheduler(interval_s=10, run_batch=work, enabled=False)
        scheduler.start()
        await asyncio.sleep(0.15)
        assert calls == []

    @pytest.mark.asyncio
    async def test_resume_wakes_it_immediately(self, stopped_scheduler):
        # Without the interruptible sleep the operator would wait a full interval.
        calls = []

        async def work():
            calls.append(1)
            return False

        scheduler = stopped_scheduler(interval_s=3600, run_batch=work, enabled=False)
        scheduler.start()
        await asyncio.sleep(0.05)
        assert calls == []

        scheduler.resume()
        await asyncio.sleep(0.15)
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_a_failing_tick_does_not_kill_the_loop(self, stopped_scheduler):
        calls = []

        async def work():
            calls.append(1)
            raise RuntimeError("MinIO unreachable")

        scheduler = stopped_scheduler(interval_s=10, run_batch=work)
        scheduler.start()
        await asyncio.sleep(0.15)

        assert scheduler.running is True
        assert scheduler.consecutive_errors >= 1
        assert "MinIO unreachable" in scheduler.last_error

    @pytest.mark.asyncio
    async def test_error_state_clears_after_a_good_tick(self, stopped_scheduler):
        outcomes = [RuntimeError("boom"), None]
        ticked = asyncio.Event()

        async def work():
            result = outcomes.pop(0) if outcomes else None
            ticked.set()
            if isinstance(result, Exception):
                raise result
            return False

        scheduler = stopped_scheduler(interval_s=10, run_batch=work)
        scheduler.start()

        await asyncio.wait_for(ticked.wait(), 1.0)
        assert scheduler.consecutive_errors == 1

        # The failure puts the loop into a 20s backoff, so wake it rather than
        # waiting it out — that backoff is the behaviour asserted above.
        ticked.clear()
        scheduler.resume()
        await asyncio.wait_for(ticked.wait(), 1.0)
        await asyncio.sleep(0.05)

        assert scheduler.last_error == ""
        assert scheduler.consecutive_errors == 0

    @pytest.mark.asyncio
    async def test_stop_ends_the_loop(self, stopped_scheduler):
        async def work():
            return False

        scheduler = stopped_scheduler(interval_s=10, run_batch=work)
        scheduler.start()
        assert scheduler.running is True
        await scheduler.stop()
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_status_reports_the_fields_the_ui_shows(self, stopped_scheduler):
        async def work():
            return False

        scheduler = stopped_scheduler(interval_s=42, run_batch=work)
        status = scheduler.status()
        assert {"enabled", "paused", "running", "interval_s", "last_error"} <= set(status)
        assert status["interval_s"] == 42


class TestImageStore:
    def test_counts_and_sizes_stored_images(self, tmp_path):
        images = tmp_path / "images"
        (images / "p1").mkdir(parents=True)
        (images / "p1" / "a.jpg").write_bytes(b"x" * 100)
        (images / "p1" / "b.png").write_bytes(b"y" * 200)

        stats = image_store(images)
        assert stats["count"] == 2
        assert stats["bytes"] == 300

    def test_partial_downloads_are_not_counted(self, tmp_path):
        # .part files are in-flight writes, not finished images.
        images = tmp_path / "images"
        (images / "p1").mkdir(parents=True)
        (images / "p1" / "a.jpg").write_bytes(b"x" * 10)
        (images / "p1" / "b.jpg.part").write_bytes(b"y" * 999)

        assert image_store(images)["count"] == 1

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert image_store(tmp_path / "nope") == {"count": 0, "bytes": 0, "mb": 0.0}
