"""The upload flow: local checkpoint, cumulative re-uploads, downloadable results.

The behaviour that matters most here is what happens on the *second* upload.
``valid_post.jsonl`` is cumulative, so re-uploading after a fresh crawl is the
normal workflow — and without a working checkpoint it would rescan everything.
"""

from __future__ import annotations

import io
import json

import pytest

from app.core.checkpoint import ProcessedCheckpoint
from app.core.config import ImageServeConfig, MinioConfig, Settings
from app.core.models import ErrorClass, HanScanError, HanScanRecord, ScannedImage
from app.core.sink import FileResultSink
from app.core.source import FileRecordSource
from app.core.uploads import UploadStore, UploadTooLarge

FRESH = "https://cdn/a.jpg?oe=7FFFFFFF"


def post_line(post_id, *, valid=True, urls=None, updated="2026-08-14"):
    return json.dumps({
        "post_id": post_id, "group_id": "g1", "is_valid": valid,
        "image_urls": urls if urls is not None else [FRESH],
        "updated_at": updated, "author": "A",
    })


def export_bytes(*lines):
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        minio=MinioConfig(),
        images=ImageServeConfig(public_base_url="https://x", signing_secret="s"),
    )


@pytest.fixture
def checkpoint(settings):
    return ProcessedCheckpoint(settings.checkpoint_path)


class TestCheckpoint:
    def test_starts_empty(self, checkpoint):
        assert checkpoint.load() == set()
        assert checkpoint.count() == 0

    def test_records_and_recalls(self, checkpoint):
        checkpoint.add(["p1", "p2"], "run1")
        assert checkpoint.contains("p1") and checkpoint.contains("p2")
        assert not checkpoint.contains("p3")

    def test_survives_a_reload(self, settings, checkpoint):
        checkpoint.add(["p1"], "run1")
        assert ProcessedCheckpoint(settings.checkpoint_path).contains("p1")

    def test_accumulates_across_runs(self, checkpoint):
        checkpoint.add(["p1"], "run1")
        checkpoint.add(["p2"], "run2")
        assert checkpoint.load() == {"p1", "p2"}

    def test_remove_allows_reclaiming(self, checkpoint):
        # Used by the retry-failed sweep.
        checkpoint.add(["p1", "p2", "p3"], "run1")
        assert checkpoint.remove(["p2"]) == 1
        assert checkpoint.load() == {"p1", "p3"}

    def test_torn_final_line_does_not_lose_earlier_ids(self, settings, checkpoint):
        checkpoint.add(["p1", "p2"], "run1")
        with open(settings.checkpoint_path, "a", encoding="utf-8") as handle:
            handle.write('{"post_id": "p3"')
        assert ProcessedCheckpoint(settings.checkpoint_path).load() == {"p1", "p2"}

    def test_empty_input_is_a_noop(self, checkpoint):
        assert checkpoint.add([], "run1") == 0
        assert checkpoint.add(["", None], "run1") == 0


class TestUploadStore:
    def test_stores_and_reads_back(self, settings):
        store = UploadStore(settings.uploads_dir)
        upload = store.save(export_bytes(post_line("p1")), "valid_post.jsonl")

        assert upload.path.exists()
        assert store.get(upload.upload_id).upload_id == upload.upload_id
        assert store.latest().upload_id == upload.upload_id

    def test_oversized_upload_is_rejected_and_cleaned_up(self, settings, monkeypatch):
        import app.core.uploads as uploads_module

        monkeypatch.setattr(uploads_module, "MAX_UPLOAD_BYTES", 10)
        store = UploadStore(settings.uploads_dir)

        with pytest.raises(UploadTooLarge):
            store.save(io.BytesIO(b"x" * 5000), "big.jsonl")

        # A rejected upload must not leave a partial file behind for the
        # scanner to pick up as if it were complete.
        assert store.list() == []

    def test_unknown_id_returns_none(self, settings):
        assert UploadStore(settings.uploads_dir).get("nope") is None

    def test_delete_removes_it(self, settings):
        store = UploadStore(settings.uploads_dir)
        upload = store.save(export_bytes(post_line("p1")), "v.jsonl")
        assert store.delete(upload.upload_id) is True
        assert store.get(upload.upload_id) is None


class TestFileRecordSource:
    def _source(self, settings, checkpoint, *lines):
        store = UploadStore(settings.uploads_dir)
        upload = store.save(export_bytes(*lines), "valid_post.jsonl")
        return FileRecordSource(upload.path, checkpoint, settings)

    def test_filters_and_dedupes(self, settings, checkpoint):
        source = self._source(
            settings, checkpoint,
            post_line("p1"),
            post_line("p2", valid=False),           # crawler-invalid
            post_line("p3", urls=[]),               # no images
            post_line("p1", updated="2026-08-15"),  # duplicate
            post_line("p4"),
        )
        batch = source.iter_pending(10)
        assert sorted(p.post_id for p in batch.posts) == ["p1", "p4"]

    def test_reports_stats_before_scanning(self, settings, checkpoint):
        source = self._source(settings, checkpoint, post_line("p1"), post_line("p2"))
        stats = source.stats()
        assert stats["scannable_posts"] == 2
        assert stats["pending_posts"] == 2
        assert stats["already_processed"] == 0
        assert stats["pending_images"] == 2

    def test_checkpoint_excludes_processed_posts(self, settings, checkpoint):
        source = self._source(settings, checkpoint, post_line("p1"), post_line("p2"))
        source.mark_done(["p1"], "run1")
        source.invalidate_cache()

        batch = source.iter_pending(10)
        assert [p.post_id for p in batch.posts] == ["p2"]
        assert source.stats()["already_processed"] == 1

    def test_reuploading_the_cumulative_export_only_scans_new_posts(
        self, settings, checkpoint
    ):
        # THE case this design exists for.
        first = self._source(settings, checkpoint, post_line("p1"), post_line("p2"))
        assert len(first.iter_pending(100).posts) == 2
        first.mark_done(["p1", "p2"], "run1")

        # Crawl again; the export now has the old posts plus two new ones.
        second = self._source(
            settings, checkpoint,
            post_line("p1"), post_line("p2"), post_line("p3"), post_line("p4"),
        )
        batch = second.iter_pending(100)

        assert sorted(p.post_id for p in batch.posts) == ["p3", "p4"]
        assert batch.corpus_total == 4
        assert batch.processed_total == 2

    def test_limit_is_respected(self, settings, checkpoint):
        source = self._source(settings, checkpoint, *[post_line(f"p{i}") for i in range(10)])
        assert len(source.iter_pending(3).posts) == 3

    def test_malformed_lines_are_counted_not_fatal(self, settings, checkpoint):
        source = self._source(settings, checkpoint, post_line("p1"), "{ broken", post_line("p2"))
        batch = source.iter_pending(10)
        assert len(batch.posts) == 2
        assert batch.malformed_lines == 1

    def test_missing_file_is_survivable(self, settings, checkpoint):
        source = FileRecordSource(settings.data_dir / "nope.jsonl", checkpoint, settings)
        assert source.iter_pending(10).posts == []


class TestFileResultSink:
    def _record(self, post_id, han_valid):
        return HanScanRecord(
            post_id=post_id, group_id="g1", han_valid=han_valid,
            han_words_total=9 if han_valid else 0, images_scanned=1,
            images=[ScannedImage(url="https://x/i", idx=0, valid_pic=han_valid)],
        )

    def test_splits_verdicts_into_two_files(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", True), self._record("p2", False)], "S1")

        assert sink.counts() == {
            "han_valid": 1, "han_invalid": 1, "ready_for_ocr": 0, "failed": 0,
        }
        assert sink.han_valid_path.exists()
        assert sink.run_path("S1", "upserts.jsonl").exists()

    def test_appends_across_batches(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", True)], "S1")
        sink.write_results([self._record("p2", True)], "S2")
        assert sink.counts()["han_valid"] == 2

    def test_errors_go_to_cumulative_and_per_run_files(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_errors([
            HanScanError(post_id="p9", group_id="g", source_url="u",
                         error_class=ErrorClass.EXPIRED_URL),
            HanScanError(post_id="p8", group_id="g", source_url="u",
                         error_class=ErrorClass.HTTP_429),
        ], "S1")

        assert sink.counts()["failed"] == 2
        assert sink.run_path("S1", "errors.jsonl").exists()
        assert [r["post_id"] for r in sink.read_failed(retryable_only=True)] == ["p8"]

    def test_output_matches_the_gemini_contract(self, settings):
        sink = FileResultSink(settings.results_dir)
        sink.write_results([self._record("p1", True)], "S1")

        row = json.loads(sink.han_valid_path.read_text(encoding="utf-8").splitlines()[0])
        assert row["stage"] == "han_scan"
        assert row["schema_version"] == "han_scan/1.1"
        assert row["scan_status"] == "scanned"
        assert row["han_valid"] is True
        assert "url" in row["images"][0]

    def test_downloadable_listing(self, settings):
        sink = FileResultSink(settings.results_dir)
        before = {d["name"]: d for d in sink.downloadable()}
        assert before["han_valid.jsonl"]["available"] is False

        sink.write_results([self._record("p1", True)], "S1")
        after = {d["name"]: d for d in sink.downloadable()}
        assert after["han_valid.jsonl"]["available"] is True
        assert after["han_valid.jsonl"]["lines"] == 1

    def test_download_names_are_allowlisted(self, settings):
        # This resolves a name arriving from a URL, so it must not path-join.
        sink = FileResultSink(settings.results_dir)
        assert sink.path_for("han_valid.jsonl") is not None
        for evil in ["../../etc/passwd", "/etc/passwd", "unknown.jsonl", ""]:
            assert sink.path_for(evil) is None
