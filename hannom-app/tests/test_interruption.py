"""Stopping mid-run must not throw away completed work.

Batches get interrupted — by a cancel, a restart, or a `docker compose down`.
What matters is that everything finished before the interruption is durable and
checkpointed, so resuming costs only what was genuinely in flight.
"""

from __future__ import annotations

import pytest

from app.core.batch import BatchRunner
from app.core.config import ImageServeConfig, MinioConfig, Settings
from app.core.downloader import DownloadResult, ImageDownloader
from app.core.jobstore import JobStore
from app.core.models import PostObject, WorkItem
from app.core.source import PendingBatch

FRESH = "https://cdn/a.jpg?oe=7FFFFFFF"


class FakeSource:
    def __init__(self, posts):
        self.posts = posts
        self.done: list[str] = []

    def iter_pending(self, limit):
        return PendingBatch(posts=self.posts[:limit], source_keys=["k"],
                            source_run_ids=["R1"], corpus_total=len(self.posts))

    def mark_done(self, post_ids, run_id):
        self.done.extend(post_ids)

    def processed_ids(self):
        return set()


class FakeSink:
    def __init__(self):
        self.results, self.errors, self.summary = [], [], None
        self.write_calls = 0

    def write_results(self, records, run_id):
        self.write_calls += 1
        self.results.extend(records)

    def write_errors(self, errors, run_id):
        self.errors.extend(errors)

    def write_run_summary(self, summary, run_id):
        self.summary = summary


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        minio=MinioConfig(),
        images=ImageServeConfig(public_base_url="https://x",
                                signing_secret="s", ttl_days=30),
    )


def _make_download(downloader, item):
    path = downloader.images_dir / f"{item.post_id}_{item.idx}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    return DownloadResult(
        item=item, ok=True, local_path=str(path), bytes_len=4,
        content_type="image/jpeg", sha256="abc", width=10, height=10,
        downloaded_at="2026-08-17T00:00:00+00:00", attempts=1,
    )


@pytest.fixture
def fake_download(monkeypatch):
    async def download_all(self, items, on_result=None, should_cancel=None):
        out = []
        for item in items:
            result = _make_download(self, item)
            out.append(result)
            if on_result:
                await on_result(result)
        return out

    monkeypatch.setattr(
        "app.core.downloader.ImageDownloader.download_all", download_all
    )


class TestIncrementalPublish:
    @pytest.mark.asyncio
    async def test_finished_posts_are_published_and_checkpointed(
        self, settings, fake_download
    ):
        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(30)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await BatchRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True
        )

        assert len(sink.results) == 30
        assert sorted(source.done) == sorted(p.post_id for p in posts)
        assert state.counts.published == 30

    @pytest.mark.asyncio
    async def test_publishing_happens_before_the_run_ends(
        self, settings, fake_download
    ):
        # 30 posts flush at the 25-download mark and again at the end, so a stop
        # in between still leaves durable results.
        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(30)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await BatchRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True
        )

        assert sink.write_calls > 1

    @pytest.mark.asyncio
    async def test_no_post_is_published_twice(self, settings, fake_download):
        posts = [
            PostObject(post_id=f"p{i}", group_id="G", is_valid=True, image_urls=[FRESH])
            for i in range(40)
        ]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await BatchRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True
        )

        ids = [r.post_id for r in sink.results]
        assert len(ids) == len(set(ids)) == 40
        assert len(source.done) == len(set(source.done))

    @pytest.mark.asyncio
    async def test_multi_image_posts_publish_only_once_complete(
        self, settings, fake_download
    ):
        # A record emitted while some of its images were still in flight would
        # under-report images_prepared.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, FRESH, FRESH])]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await BatchRunner(settings, source, sink).run(
            job_dir, state, confirm_expired=True
        )

        assert len(sink.results) == 1
        assert sink.results[0].images_prepared == 3


class TestDownloadReuse:
    """An interrupted run must be cheap to redo — and the CDN URLs may have
    expired since the first attempt, so re-fetching is not always possible."""

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
