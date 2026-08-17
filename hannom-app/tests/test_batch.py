"""Batch orchestration: the expiry gate, record assembly, and fault isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import ocr as ocr_module
from app.core.batch import EXPIRY_GATE_RATIO, BatchRunner
from app.core.config import ImageServeConfig, MinioConfig, OcrConfig, Settings
from app.core.downloader import DownloadResult
from app.core.jobstore import JobStore, Phase
from app.core.models import ErrorClass, PostObject, ScanOutcome, WorkItem
from app.core.source import PendingBatch

FRESH = "https://cdn/a.jpg?oe=7FFFFFFF"   # 2038
DEAD = "https://cdn/b.jpg?oe=5FFFFFFF"    # 2020


class FakeSource:
    def __init__(self, posts):
        self.posts = posts
        self.done: list[str] = []

    def iter_pending(self, limit):
        return PendingBatch(
            posts=self.posts[:limit],
            source_keys=["logs/by_run/R1/upserts.jsonl"],
            source_run_ids=["R1"], corpus_total=100, processed_total=5,
            runs_total=3, runs_done=1, current_run_id="R1",
        )

    def mark_done(self, post_ids, scan_run_id):
        self.done.extend(post_ids)

    def processed_ids(self):
        return set()


class FakeSink:
    def __init__(self):
        self.results, self.errors, self.summary = [], [], None

    def write_results(self, records, scan_run_id):
        self.results.extend(records)

    def write_errors(self, errors, scan_run_id):
        self.errors.extend(errors)

    def write_run_summary(self, summary, scan_run_id):
        self.summary = summary


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        minio=MinioConfig(group_prefix="facebook/G"),
        ocr=OcrConfig(workers=2, timeout_s=2.0, memory_limit_mb=0),
        images=ImageServeConfig(
            public_base_url="https://scan.example.com",
            signing_secret="test-secret", ttl_days=30,
        ),
    )


def fake_download(tmp_path):
    """Succeeds unless the URL is the dead one."""

    async def download_all(self, items, on_result=None, should_cancel=None):
        out = []
        for item in items:
            if "b.jpg" in item.source_url:
                result = DownloadResult(
                    item=item, ok=False, attempts=1,
                    error_class=ErrorClass.EXPIRED_URL, error_detail="expired",
                )
            else:
                path = self.images_dir / f"{item.post_id}_{item.idx}.jpg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake-jpeg")
                result = DownloadResult(
                    item=item, ok=True, local_path=str(path), bytes_len=9,
                    content_type="image/jpeg", sha256="deadbeef",
                    width=800, height=600, downloaded_at="2026-08-16T00:00:00+00:00",
                    attempts=1,
                )
            out.append(result)
            if on_result:
                await on_result(result)
        return out

    return download_all


class StubOcr(BatchRunner):
    """Image 0 has Han, image 1 does not — deterministic, no PaddleOCR needed."""

    async def _phase_ocr(self, job_dir, state, downloads, on_chunk=None):
        state.phase = Phase.OCR
        outcomes = {}
        for key, download in downloads.items():
            if not download.ok:
                continue
            has_han = download.item.idx == 0
            outcome = ScanOutcome(
                idx=download.item.idx, post_id=download.item.post_id, ok=True,
                valid_pic=has_han, han_words=12 if has_han else 0,
                boxes=3, texts=["平定"] if has_han else ["abc"],
                mean_confidence=0.94, scan_ms=120,
            )
            outcomes[key] = outcome
            state.counts.scanned += 1
        return outcomes


@pytest.fixture
def runner_factory(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.core.downloader.ImageDownloader.download_all", fake_download(tmp_path)
    )

    def build(posts):
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("scan_R1", limit=100)
        return StubOcr(settings, source, sink), job_dir, state, source, sink

    return build


class TestExpiryGate:
    @pytest.mark.asyncio
    async def test_pauses_when_too_many_urls_are_dead(self, runner_factory):
        # Scanning mostly-dead links for hours is worse than re-crawling.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        runner, job_dir, state, _, sink = runner_factory(posts)

        state = await runner.run(job_dir, state, confirm_expired=False)

        assert state.awaiting_confirmation is True
        assert state.phase is Phase.PENDING
        assert sink.results == []

    @pytest.mark.asyncio
    async def test_confirmation_overrides_the_gate(self, runner_factory):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        runner, job_dir, state, _, sink = runner_factory(posts)

        state = await runner.run(job_dir, state, confirm_expired=True)

        assert state.awaiting_confirmation is False
        assert state.phase is Phase.DONE
        assert len(sink.errors) == 1

    @pytest.mark.asyncio
    async def test_fresh_urls_pass_straight_through(self, runner_factory):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        runner, job_dir, state, _, _ = runner_factory(posts)

        state = await runner.run(job_dir, state, confirm_expired=False)
        assert state.phase is Phase.DONE

    @pytest.mark.asyncio
    async def test_no_work_finishes_cleanly(self, runner_factory):
        runner, job_dir, state, _, _ = runner_factory([])
        state = await runner.run(job_dir, state)
        assert state.phase is Phase.DONE


class TestRecordAssembly:
    @pytest.mark.asyncio
    async def test_han_valid_is_true_when_any_image_has_han(self, runner_factory):
        # Image 1 has no Han; the post still counts because image 0 does.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, FRESH], author="A", label="L")]
        runner, job_dir, state, source, sink = runner_factory(posts)
        await runner.run(job_dir, state, confirm_expired=True)

        record = sink.results[0]
        assert record.han_valid is True
        assert record.images_scanned == 2
        assert [i.valid_pic for i in record.images] == [True, False]
        assert record.han_words_total == 12

    @pytest.mark.asyncio
    async def test_image_urls_are_signed_and_verifiable(self, runner_factory):
        from urllib.parse import parse_qs, urlparse
        from app.core.signing import verify

        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        runner, job_dir, state, _, sink = runner_factory(posts)
        await runner.run(job_dir, state, confirm_expired=True)

        url = sink.results[0].images[0].url
        params = parse_qs(urlparse(url).query)
        assert verify("p1", 0, int(params["exp"][0]), params["sig"][0], "test-secret")

    @pytest.mark.asyncio
    async def test_failed_downloads_become_errors_not_records(self, runner_factory):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        runner, job_dir, state, _, sink = runner_factory(posts)
        await runner.run(job_dir, state, confirm_expired=True)

        assert sink.results == []
        assert sink.errors[0].error_class is ErrorClass.EXPIRED_URL
        assert sink.errors[0].retryable is False

    @pytest.mark.asyncio
    async def test_posts_are_marked_done_only_after_publishing(self, runner_factory):
        # Marking earlier would silently drop posts if publishing failed.
        posts = [
            PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH]),
            PostObject(post_id="p2", group_id="G", is_valid=True, image_urls=[DEAD]),
        ]
        runner, job_dir, state, source, sink = runner_factory(posts)
        await runner.run(job_dir, state, confirm_expired=True)

        assert source.done == ["p1"]   # p2 never produced a record

    @pytest.mark.asyncio
    async def test_run_summary_is_written(self, runner_factory):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        runner, job_dir, state, _, sink = runner_factory(posts)
        await runner.run(job_dir, state, confirm_expired=True)

        assert sink.summary["counts"]["published"] == 1
        assert "preflight" in sink.summary


class TestOcrFaultIsolation:
    """A poison image must cost one item, never the batch."""

    @staticmethod
    def _download(post_id, tmp_path):
        path = tmp_path / f"{post_id}.jpg"
        path.write_bytes(b"x")
        return DownloadResult(
            item=WorkItem(post_id=post_id, idx=0, source_url="u"),
            ok=True, local_path=str(path), attempts=1,
        )

    @pytest.fixture
    def thread_runner(self, settings):
        class ThreadRunner(BatchRunner):
            def _new_pool(self, workers):
                return ThreadPoolExecutor(max_workers=workers)

        return ThreadRunner(settings, None, None)

    @pytest.mark.asyncio
    async def test_engine_error_on_one_image_does_not_stop_the_rest(
        self, thread_runner, settings, monkeypatch, tmp_path
    ):
        def scan_file(path, post_id, idx, **kwargs):
            if "poison" in post_id:
                raise RuntimeError("simulated engine explosion")
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, valid_pic=True,
                               han_words=5, boxes=1, texts=["好"], scan_ms=3)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("r", limit=10)
        downloads = {
            f"{name}:0": self._download(name, tmp_path)
            for name in ("ok1", "poison1", "ok2")
        }

        outcomes = await thread_runner._phase_ocr(job_dir, state, downloads)

        assert len(outcomes) == 3
        assert outcomes["ok1:0"].ok and outcomes["ok2:0"].ok
        assert outcomes["poison1:0"].ok is False
        assert outcomes["poison1:0"].error_class is ErrorClass.OCR_ERROR
        assert state.counts.scanned == 2 and state.counts.scan_failed == 1

    @pytest.mark.asyncio
    async def test_slow_image_times_out_instead_of_stalling(
        self, settings, monkeypatch, tmp_path
    ):
        import time

        def scan_file(path, post_id, idx, **kwargs):
            if "slow" in post_id:
                time.sleep(5)
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        fast_timeout = Settings(
            data_dir=settings.data_dir, minio=settings.minio,
            ocr=OcrConfig(workers=2, timeout_s=0.4, memory_limit_mb=0),
            images=settings.images,
        )

        class ThreadRunner(BatchRunner):
            def _new_pool(self, workers):
                return ThreadPoolExecutor(max_workers=workers)

        store = JobStore(fast_timeout.jobs_dir)
        job_dir, state = store.create("r", limit=10)
        downloads = {
            "slowone:0": self._download("slowone", tmp_path),
            "fast:0": self._download("fast", tmp_path),
        }

        outcomes = await ThreadRunner(fast_timeout, None, None)._phase_ocr(
            job_dir, state, downloads
        )

        assert outcomes["fast:0"].ok is True
        assert outcomes["slowone:0"].ok is False
        assert "timed out" in outcomes["slowone:0"].error_detail

    @pytest.mark.asyncio
    async def test_already_scanned_images_are_not_rescanned(
        self, thread_runner, settings, monkeypatch, tmp_path
    ):
        calls = []

        def scan_file(path, post_id, idx, **kwargs):
            calls.append(post_id)
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("r", limit=10)
        downloads = {f"p{i}:0": self._download(f"p{i}", tmp_path) for i in range(4)}

        await thread_runner._phase_ocr(job_dir, state, downloads)
        assert len(calls) == 4

        calls.clear()
        state.counts.scanned = 0
        outcomes = await thread_runner._phase_ocr(job_dir, state, downloads)

        assert calls == []                 # nothing rescanned
        assert len(outcomes) == 4          # prior results still returned
        assert len(job_dir.read_results()) == 4   # and not duplicated on disk
