"""Stopping mid-run must not throw away completed work.

A multi-hour OCR batch will get interrupted — by a cancel, a restart, or a
`docker compose down`. What matters is that everything finished before the
interruption is durable and checkpointed, so resuming costs only the work that
was genuinely in flight.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import ocr as ocr_module
from app.core.batch import BatchRunner
from app.core.config import ImageServeConfig, MinioConfig, OcrConfig, Settings
from app.core.downloader import DownloadResult, ImageDownloader
from app.core.jobstore import JobStore
from app.core.models import PostObject, ScanOutcome, WorkItem
from app.core.source import PendingBatch

FRESH = "https://cdn/a.jpg?oe=7FFFFFFF"


class FakeSource:
    def __init__(self, posts):
        self.posts = posts
        self.done: list[str] = []

    def iter_pending(self, limit):
        return PendingBatch(posts=self.posts[:limit], source_keys=["k"],
                            source_run_ids=["R1"], corpus_total=len(self.posts))

    def mark_done(self, post_ids, scan_run_id):
        self.done.extend(post_ids)

    def processed_ids(self):
        return set()


class FakeSink:
    def __init__(self):
        self.results, self.errors, self.summary = [], [], None
        self.write_calls = 0

    def write_results(self, records, scan_run_id):
        self.write_calls += 1
        self.results.extend(records)

    def write_errors(self, errors, scan_run_id):
        self.errors.extend(errors)

    def write_run_summary(self, summary, scan_run_id):
        self.summary = summary


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        minio=MinioConfig(),
        # workers=1 makes chunk boundaries deterministic (chunk = workers * 4)
        ocr=OcrConfig(workers=1, timeout_s=5.0, memory_limit_mb=0),
        images=ImageServeConfig(public_base_url="https://x",
                                signing_secret="s", ttl_days=30),
    )


@pytest.fixture
def fake_download(monkeypatch):
    async def download_all(self, items, on_result=None, should_cancel=None):
        out = []
        for item in items:
            path = self.images_dir / f"{item.post_id}_{item.idx}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake")
            result = DownloadResult(
                item=item, ok=True, local_path=str(path), bytes_len=4,
                content_type="image/jpeg", sha256="abc", width=10, height=10,
                downloaded_at="2026-08-17T00:00:00+00:00", attempts=1,
            )
            out.append(result)
            if on_result:
                await on_result(result)
        return out

    monkeypatch.setattr(
        "app.core.downloader.ImageDownloader.download_all", download_all
    )


class ThreadRunner(BatchRunner):
    def _new_pool(self, workers):
        return ThreadPoolExecutor(max_workers=workers)


class TestIncrementalPublish:
    @pytest.mark.asyncio
    async def test_results_are_published_during_ocr_not_only_at_the_end(
        self, settings, fake_download, monkeypatch
    ):
        def scan_file(path, post_id, idx, **kwargs):
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, valid_pic=True,
                               han_words=3, boxes=1, texts=["文"], scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(12)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await ThreadRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True, run_ocr=True
        )

        # 12 posts at chunk size 4 => several flushes, not a single final write.
        assert sink.write_calls > 1
        assert len(sink.results) == 12
        assert sorted(source.done) == sorted(p.post_id for p in posts)

    @pytest.mark.asyncio
    async def test_cancelling_keeps_everything_already_finished(
        self, settings, fake_download, monkeypatch
    ):
        scanned = {"n": 0}

        def scan_file(path, post_id, idx, **kwargs):
            scanned["n"] += 1
            if scanned["n"] > 4:
                state_holder["state"].cancel_requested = True
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, valid_pic=True,
                               han_words=2, boxes=1, texts=["文"], scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(20)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)
        state_holder = {"state": state}

        await ThreadRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True, run_ocr=True
        )

        # Partial, but real: what finished is published AND checkpointed, so a
        # later run does not repeat it.
        assert 0 < len(sink.results) < 20
        assert sorted(source.done) == sorted(r.post_id for r in sink.results)

    @pytest.mark.asyncio
    async def test_no_post_is_published_twice(
        self, settings, fake_download, monkeypatch
    ):
        def scan_file(path, post_id, idx, **kwargs):
            return ScanOutcome(idx=idx, post_id=post_id, ok=True, valid_pic=True,
                               han_words=1, boxes=1, texts=["x"], scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(10)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await ThreadRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True, run_ocr=True
        )

        ids = [r.post_id for r in sink.results]
        assert len(ids) == len(set(ids)) == 10
        assert len(source.done) == len(set(source.done))
        assert state.counts.published == 10

    @pytest.mark.asyncio
    async def test_multi_image_posts_publish_only_once_complete(
        self, settings, fake_download, monkeypatch
    ):
        # A post published after only some of its images were scanned would
        # carry a han_valid computed from part of the evidence.
        def scan_file(path, post_id, idx, **kwargs):
            return ScanOutcome(idx=idx, post_id=post_id, ok=True,
                               valid_pic=(idx == 2), han_words=5 if idx == 2 else 0,
                               boxes=1, texts=["文"], scan_ms=1)

        monkeypatch.setattr(ocr_module, "scan_file", scan_file)

        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, FRESH, FRESH])]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await ThreadRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True, run_ocr=True
        )

        assert len(sink.results) == 1
        record = sink.results[0]
        assert record.images_scanned == 3
        assert record.han_valid is True   # the third image carried the Han text


class TestDownloadReuse:
    def test_existing_file_is_found(self, settings):
        downloader = ImageDownloader(settings.download, settings.images_dir)
        path = downloader.local_path_for("p1", 0, ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

        assert downloader.existing_file("p1", 0) == path

    def test_other_suffixes_are_found(self, settings):
        downloader = ImageDownloader(settings.download, settings.images_dir)
        path = downloader.local_path_for("p1", 0, ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

        assert downloader.existing_file("p1", 0) == path

    def test_empty_file_is_not_reused(self, settings):
        # A zero-byte file is a failed write, not a usable image.
        downloader = ImageDownloader(settings.download, settings.images_dir)
        path = downloader.local_path_for("p1", 0, ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

        assert downloader.existing_file("p1", 0) is None

    def test_missing_file_returns_none(self, settings):
        downloader = ImageDownloader(settings.download, settings.images_dir)
        assert downloader.existing_file("nope", 0) is None

    def test_rebuilt_result_carries_usable_metadata(self, settings):
        downloader = ImageDownloader(settings.download, settings.images_dir)
        path = downloader.local_path_for("p1", 0, ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-real-jpeg")

        result = downloader._from_disk(
            WorkItem(post_id="p1", idx=0, source_url="u"), path
        )
        assert result.ok is True
        assert result.local_path == str(path)
        assert result.bytes_len == 15
        assert result.sha256
        assert result.attempts == 0   # marks it as "not fetched this run"
