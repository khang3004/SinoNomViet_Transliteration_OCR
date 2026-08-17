"""Crash survivability: append-only logs, resume, and interrupted-job reaping."""

from __future__ import annotations

import json
import os
import time

import pytest

from app.core.jobstore import (
    HEARTBEAT_STALE_S,
    JobDir,
    JobState,
    JobStore,
    Phase,
    new_job_id,
    new_run_id,
)


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs")


class TestIds:
    def test_run_id_sorts_chronologically(self):
        # Same timestamp shape as the crawler's run ids, so both logs sort together.
        assert len(new_run_id()) == 15 and "T" in new_run_id()

    def test_job_ids_are_unique(self):
        assert len({new_job_id() for _ in range(50)}) == 50


class TestPhase:
    @pytest.mark.parametrize(
        "phase", [Phase.DONE, Phase.FAILED, Phase.CANCELLED, Phase.INTERRUPTED]
    )
    def test_terminal_phases(self, phase):
        assert phase.terminal is True

    @pytest.mark.parametrize(
        "phase", [Phase.PENDING, Phase.PREFLIGHT, Phase.DOWNLOAD, Phase.OCR, Phase.PUBLISH]
    )
    def test_running_phases(self, phase):
        assert phase.terminal is False


class TestStatePersistence:
    def test_state_roundtrips(self, store):
        job_dir, state = store.create("run1", limit=500)
        state.phase = Phase.OCR
        state.counts.scanned = 42
        state.preflight = {"total": 10, "expired": 1}
        job_dir.save_state(state)

        loaded = job_dir.load_state()
        assert loaded.phase is Phase.OCR
        assert loaded.counts.scanned == 42
        assert loaded.preflight["expired"] == 1

    def test_save_is_atomic(self, store):
        # A half-written job.json would make a job unreadable after a crash.
        job_dir, state = store.create("run1", limit=10)
        for i in range(20):
            state.counts.scanned = i
            job_dir.save_state(state)
            json.loads(job_dir.job_file.read_text(encoding="utf-8"))

    def test_unreadable_state_returns_none_rather_than_raising(self, store):
        job_dir, _ = store.create("run1", limit=10)
        job_dir.job_file.write_text("{ corrupt", encoding="utf-8")
        assert job_dir.load_state() is None


class TestResume:
    def test_completed_work_is_skipped_on_replay(self, store):
        job_dir, _ = store.create("run1", limit=10)
        job_dir.write_manifest(
            [{"post_id": f"p{i}", "idx": 0, "source_url": f"u{i}"} for i in range(5)]
        )
        for i in range(3):
            job_dir.append_download({"post_id": f"p{i}", "idx": 0, "ok": True})
        for i in range(2):
            job_dir.append_result({"post_id": f"p{i}", "idx": 0, "ok": True})

        done_downloads = job_dir.completed_download_keys()
        done_scans = job_dir.completed_scan_keys()
        remaining = [
            m for m in job_dir.read_manifest()
            if f"{m['post_id']}:{m['idx']}" not in done_scans
        ]

        assert set(done_downloads) == {"p0:0", "p1:0", "p2:0"}
        assert [m["post_id"] for m in remaining] == ["p2", "p3", "p4"]

    def test_manifest_is_not_mutated_by_progress(self, store):
        job_dir, _ = store.create("run1", limit=10)
        original = [{"post_id": "p1", "idx": 0, "source_url": "u"}]
        job_dir.write_manifest(original)
        job_dir.append_result({"post_id": "p1", "idx": 0, "ok": True})
        assert job_dir.read_manifest() == original

    def test_torn_final_line_does_not_lose_earlier_records(self, store):
        # A hard kill mid-write leaves a partial line; everything before it stands.
        job_dir, _ = store.create("run1", limit=10)
        for i in range(3):
            job_dir.append_result({"post_id": f"p{i}", "idx": 0, "ok": True})
        with open(job_dir.results_file, "a", encoding="utf-8") as handle:
            handle.write('{"post_id": "p3", "idx": tru')

        assert len(job_dir.read_results()) == 3


class TestEventCursor:
    def test_cursor_returns_only_unseen_lines(self, store):
        job_dir, _ = store.create("run1", limit=10)
        baseline = job_dir.event_count()  # creation logs one event
        for i in range(5):
            job_dir.append_event("info", f"event {i}")

        first, cursor = job_dir.read_events(baseline, 3)
        assert [e["message"] for e in first] == ["event 0", "event 1", "event 2"]

        second, cursor = job_dir.read_events(cursor, 3)
        assert [e["message"] for e in second] == ["event 3", "event 4"]

        empty, final = job_dir.read_events(cursor, 3)
        assert empty == [] and final == cursor

    def test_levels_are_recorded(self, store):
        job_dir, _ = store.create("run1", limit=10)
        job_dir.append_event("warn", "careful")
        job_dir.append_event("error", "broken")
        events, _ = job_dir.read_events(0, 100)
        assert {e["level"] for e in events} >= {"warn", "error"}


class TestInterruptedJobs:
    def test_fresh_job_is_not_stale(self, store):
        job_dir, state = store.create("run1", limit=10)
        state.phase = Phase.OCR
        job_dir.save_state(state)
        assert job_dir.is_stale() is False
        assert store.reap_interrupted() == []

    def test_dead_job_is_reaped_and_offers_resume(self, store):
        job_dir, state = store.create("run1", limit=10)
        state.phase = Phase.OCR
        job_dir.save_state(state)

        old = time.time() - HEARTBEAT_STALE_S - 10
        os.utime(job_dir.job_file, (old, old))

        assert store.reap_interrupted() == [job_dir.job_id]
        assert job_dir.load_state().phase is Phase.INTERRUPTED
        assert store.active() is None

    def test_terminal_jobs_are_left_alone(self, store):
        job_dir, state = store.create("run1", limit=10)
        state.phase = Phase.DONE
        job_dir.save_state(state)
        old = time.time() - HEARTBEAT_STALE_S - 10
        os.utime(job_dir.job_file, (old, old))

        assert store.reap_interrupted() == []
        assert job_dir.load_state().phase is Phase.DONE


class TestStoreQueries:
    def test_lists_newest_first(self, store):
        ids = [store.create(f"run{i}", limit=1)[1].job_id for i in range(3)]
        assert store.list_ids()[0] == sorted(ids, reverse=True)[0]

    def test_active_finds_the_running_job(self, store):
        job_dir, state = store.create("run1", limit=10)
        state.phase = Phase.DOWNLOAD
        job_dir.save_state(state)
        assert store.active().job_id == state.job_id

    def test_no_active_job_when_all_terminal(self, store):
        job_dir, state = store.create("run1", limit=10)
        state.phase = Phase.DONE
        job_dir.save_state(state)
        assert store.active() is None
