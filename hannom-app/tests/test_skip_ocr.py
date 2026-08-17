"""Prepare-only mode: download and sign, but never scan.

The property under test throughout is that "not scanned" is never representable
as "no Han text found". A downstream consumer reading han_valid=false would drop
those posts believing they had been checked.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import ImageServeConfig, MinioConfig, OcrConfig, Settings
from app.core.downloader import DownloadResult
from app.core.jobstore import JobStore, Phase
from app.core.models import SCAN_SCANNED, SCAN_SKIPPED, ErrorClass, PostObject, ScanOutcome
from app.core.signing import verify
from app.core.sink import FileResultSink
from app.core.source import PendingBatch
from app.core.batch import BatchRunner

FRESH = "https://cdn/a.jpg?oe=7FFFFFFF"
DEAD = "https://cdn/b.jpg?oe=5FFFFFFF"


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
        minio=MinioConfig(),
        ocr=OcrConfig(workers=1, memory_limit_mb=0),
        images=ImageServeConfig(public_base_url="https://scan.example.com",
                                signing_secret="test-secret", ttl_days=30),
    )


@pytest.fixture
def fake_download(monkeypatch):
    async def download_all(self, items, on_result=None, should_cancel=None):
        out = []
        for item in items:
            if "b.jpg" in item.source_url:
                result = DownloadResult(item=item, ok=False, attempts=1,
                                        error_class=ErrorClass.EXPIRED_URL,
                                        error_detail="expired")
            else:
                path = self.images_dir / f"{item.post_id}_{item.idx}.jpg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake")
                result = DownloadResult(
                    item=item, ok=True, local_path=str(path), bytes_len=4,
                    content_type="image/jpeg", sha256="abc", width=800, height=600,
                    downloaded_at="2026-08-17T00:00:00+00:00", attempts=1,
                )
            out.append(result)
            if on_result:
                await on_result(result)
        return out

    monkeypatch.setattr(
        "app.core.downloader.ImageDownloader.download_all", download_all
    )


class NeverOcr(BatchRunner):
    """Fails loudly if the OCR phase is entered at all."""

    async def _phase_ocr(self, job_dir, state, downloads, on_chunk=None):
        raise AssertionError("OCR phase must not run when run_ocr=False")


@pytest.fixture
def run_prepare(settings, fake_download):
    async def go(posts, runner_cls=NeverOcr):
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)
        runner = runner_cls(settings, source, sink)
        state = await runner.run(
            job_dir, state, confirm_expired=True, run_ocr=False
        )
        return state, source, sink, job_dir

    return go


class TestOcrIsSkipped:
    @pytest.mark.asyncio
    async def test_ocr_phase_never_runs(self, run_prepare):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        state, _, _, _ = await run_prepare(posts)
        assert state.phase is Phase.DONE  # NeverOcr would have failed the batch

    @pytest.mark.asyncio
    async def test_job_records_the_choice(self, run_prepare):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        state, _, _, job_dir = await run_prepare(posts)
        assert state.run_ocr is False
        assert job_dir.load_state().run_ocr is False


class TestUnscannedRecords:
    @pytest.mark.asyncio
    async def test_verdict_fields_are_null_not_false(self, run_prepare):
        # The whole point: false would mean "checked, found nothing".
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_prepare(posts)

        record = sink.results[0]
        assert record.scan_status == SCAN_SKIPPED
        assert record.han_valid is None
        assert record.han_words_total is None
        assert record.images[0].valid_pic is None
        assert record.images[0].han_words is None

    @pytest.mark.asyncio
    async def test_no_engine_is_claimed(self, run_prepare):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_prepare(posts)
        assert sink.results[0].ocr_engine == ""

    @pytest.mark.asyncio
    async def test_images_still_get_working_signed_urls(self, run_prepare):
        # The signed URL is the entire deliverable of this mode.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True,
                            image_urls=[FRESH, FRESH])]
        _, _, sink, _ = await run_prepare(posts)

        record = sink.results[0]
        assert record.images_scanned == 2
        for image in record.images:
            params = parse_qs(urlparse(image.url).query)
            assert verify("p1", image.idx, int(params["exp"][0]),
                          params["sig"][0], "test-secret")

    @pytest.mark.asyncio
    async def test_download_failures_are_still_errors(self, run_prepare):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[DEAD])]
        _, _, sink, _ = await run_prepare(posts)
        assert sink.results == []
        assert sink.errors[0].error_class is ErrorClass.EXPIRED_URL

    @pytest.mark.asyncio
    async def test_posts_are_marked_done(self, run_prepare):
        # They are finished as far as THIS stage is concerned.
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, source, _, _ = await run_prepare(posts)
        assert source.done == ["p1"]

    @pytest.mark.asyncio
    async def test_summary_records_that_ocr_was_skipped(self, run_prepare):
        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        _, _, sink, _ = await run_prepare(posts)
        assert sink.summary["ocr_run"] is False
        assert sink.summary["counts"]["ready_for_ocr"] == 1


class TestSinkRouting:
    def _record(self, post_id, *, status, han_valid):
        from app.core.models import HanScanRecord, ScannedImage

        return HanScanRecord(
            post_id=post_id, group_id="g", scan_status=status, han_valid=han_valid,
            images=[ScannedImage(url="https://x/i", idx=0,
                                 valid_pic=None if status == SCAN_SKIPPED else han_valid)],
        )

    def test_skipped_records_go_to_their_own_file(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([
            self._record("p1", status=SCAN_SKIPPED, han_valid=None),
            self._record("p2", status=SCAN_SCANNED, han_valid=True),
            self._record("p3", status=SCAN_SCANNED, han_valid=False),
        ], "S1")

        counts = sink.counts()
        assert counts["ready_for_ocr"] == 1
        assert counts["han_valid"] == 1
        assert counts["han_invalid"] == 1

    def test_skipped_never_pollute_han_invalid(self, settings):
        # A skipped record landing in han_invalid.jsonl would be silently wrong.
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", status=SCAN_SKIPPED, han_valid=None)], "S1")

        assert sink.counts()["han_invalid"] == 0
        assert sink.ready_for_ocr_path.exists()
        assert not sink.han_invalid_path.exists()

    def test_ready_file_is_downloadable(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", status=SCAN_SKIPPED, han_valid=None)], "S1")

        entry = {d["name"]: d for d in sink.downloadable()}["ready_for_ocr.jsonl"]
        assert entry["available"] is True and entry["lines"] == 1
        assert sink.path_for("ready_for_ocr.jsonl") is not None

    def test_serialised_shape_marks_it_unscanned(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", status=SCAN_SKIPPED, han_valid=None)], "S1")

        row = json.loads(sink.ready_for_ocr_path.read_text(encoding="utf-8").strip())
        assert row["scan_status"] == "skipped"
        assert row["han_valid"] is None
        assert row["images"][0]["valid_pic"] is None
        assert row["schema_version"] == "han_scan/1.1"


class TestOcrStillWorks:
    """Prepare+OCR must be unaffected by any of the above."""

    @pytest.mark.asyncio
    async def test_scanned_records_keep_real_verdicts(self, settings, fake_download):
        class StubOcr(BatchRunner):
            async def _phase_ocr(self, job_dir, state, downloads, on_chunk=None):
                out = {}
                for key, download in downloads.items():
                    if not download.ok:
                        continue
                    out[key] = ScanOutcome(
                        idx=download.item.idx, post_id=download.item.post_id,
                        ok=True, valid_pic=True, han_words=8, boxes=2,
                        texts=["平定"], mean_confidence=0.9, scan_ms=50,
                    )
                    state.counts.scanned += 1
                return out

        posts = [PostObject(post_id="p1", group_id="G", is_valid=True, image_urls=[FRESH])]
        source, sink = FakeSource(posts), FakeSink()
        store = JobStore(settings.jobs_dir)
        job_dir, state = store.create("S1", limit=100)

        await StubOcr(settings, source, sink).run(
            job_dir, state, confirm_expired=True, run_ocr=True
        )

        record = sink.results[0]
        assert record.scan_status == SCAN_SCANNED
        assert record.han_valid is True
        assert record.han_words_total == 8
        assert record.images[0].valid_pic is True
        assert record.ocr_engine  # an engine label is claimed here
