"""Batch orchestration: the expiry gate and record assembly."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.batch import EXPIRY_GATE_RATIO, BatchRunner
from app.core.config import ImageServeConfig, MinioConfig, Settings
from app.core.downloader import DownloadResult
from app.core.jobstore import JobStore, Phase
from app.core.models import ErrorClass, PostObject
from app.core.signing import verify
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
        minio=MinioConfig(group_prefix="facebook/G"),
        images=ImageServeConfig(
            public_base_url="https://scan.example.com",
            signing_secret="test-secret", ttl_days=30,
        ),
    )


@pytest.fixture
def fake_download(monkeypatch):
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
                    width=800, height=600,
                    downloaded_at="2026-08-17T00:00:00+00:00", attempts=1,
                )
            out.append(result)
            if on_result:
                await on_result(result)
        return out

    monkeypatch.setattr(
        "app.core.downloader.ImageDownloader.download_all", download_all
    )


@pytest.fixture
def run_batch(settings, fake_download):
    async def go(posts, confirm_expired=True):
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("R1", limit=100)
        runner = BatchRunner(settings, source, sink)
        state = await runner.run(job_dir, state, confirm_expired=confirm_expired)
        return state, source, sink, job_dir

    return go


class TestExpiryGate:
    @pytest.mark.asyncio
    async def test_pauses_when_too_many_urls_are_dead(self, run_batch):
        # Downloading mostly-dead links produces failures, not images.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        state, _, sink, _ = await run_batch(posts, confirm_expired=False)

        assert state.awaiting_confirmation is True
        assert state.phase is Phase.PENDING
        assert sink.results == []

    @pytest.mark.asyncio
    async def test_confirmation_overrides_the_gate(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        state, _, sink, _ = await run_batch(posts)

        assert state.awaiting_confirmation is False
        assert state.phase is Phase.DONE
        assert len(sink.errors) == 1

    @pytest.mark.asyncio
    async def test_fresh_urls_pass_straight_through(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        state, _, _, _ = await run_batch(posts, confirm_expired=False)
        assert state.phase is Phase.DONE

    @pytest.mark.asyncio
    async def test_no_work_finishes_cleanly(self, run_batch):
        state, _, _, _ = await run_batch([])
        assert state.phase is Phase.DONE

    def test_gate_ratio_is_a_minority_threshold(self):
        # A handful of dead links in a fresh crawl should not stop a run.
        assert 0 < EXPIRY_GATE_RATIO < 0.5


class TestRecordAssembly:
    @pytest.mark.asyncio
    async def test_every_image_is_prepared(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, FRESH], author="A", label="L")]
        _, _, sink, _ = await run_batch(posts)

        record = sink.results[0]
        assert record.images_prepared == 2
        assert [i.idx for i in record.images] == [0, 1]
        assert record.author == "A" and record.label == "L"

    @pytest.mark.asyncio
    async def test_image_urls_are_signed_and_verifiable(self, run_batch):
        # The signed URL is the entire deliverable of this stage.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_batch(posts)

        url = sink.results[0].images[0].url
        params = parse_qs(urlparse(url).query)
        assert verify("p1", 0, int(params["exp"][0]), params["sig"][0], "test-secret")

    @pytest.mark.asyncio
    async def test_dimensions_survive_from_the_downloader(self, run_batch):
        # image_dimensions() moved out of the deleted OCR module; if that
        # relocation broke, every record silently loses width/height.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_batch(posts)

        image = sink.results[0].images[0]
        assert (image.width, image.height) == (800, 600)
        assert image.sha256 and image.bytes == 9

    @pytest.mark.asyncio
    async def test_provenance_is_recorded(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_batch(posts)

        image = sink.results[0].images[0]
        assert image.source_url == FRESH
        assert image.source_expires_at  # decoded from the oe param
        assert image.url_expires_at     # our own signature expiry

    @pytest.mark.asyncio
    async def test_carries_no_content_verdict(self, run_batch):
        # Schema 2.0: this stage makes no claim about what is in the image.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_batch(posts)

        row = sink.results[0].to_json()
        for gone in ("han_valid", "han_words_total", "scan_status", "ocr_engine"):
            assert gone not in row
        assert row["schema_version"] == "han_scan/2.0"
        assert "valid_pic" not in row["images"][0]

    @pytest.mark.asyncio
    async def test_failed_downloads_become_errors_not_records(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        _, _, sink, _ = await run_batch(posts)

        assert sink.results == []
        assert sink.errors[0].error_class is ErrorClass.EXPIRED_URL
        assert sink.errors[0].retryable is False

    @pytest.mark.asyncio
    async def test_partial_post_keeps_the_images_that_worked(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, DEAD])]
        _, _, sink, _ = await run_batch(posts)

        record = sink.results[0]
        assert record.images_prepared == 1
        assert record.images_failed == 1
        assert len(sink.errors) == 1

    @pytest.mark.asyncio
    async def test_posts_are_marked_done_only_after_publishing(self, run_batch):
        # Marking earlier would silently drop posts if publishing failed.
        posts = [
            PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH]),
            PostObject(post_id="p2", group_id="G", is_valid=True, image_urls=[DEAD]),
        ]
        _, source, _, _ = await run_batch(posts)

        assert source.done == ["p1"]   # p2 never produced a record

    @pytest.mark.asyncio
    async def test_run_summary_is_written(self, run_batch):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_batch(posts)

        assert sink.summary["counts"]["published"] == 1
        assert "preflight" in sink.summary
