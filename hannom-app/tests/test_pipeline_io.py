"""The RecordSource / ResultSink contracts, against an in-memory object store.

These are the boundaries the crawl team can swap, so the tests exercise them
through the protocols rather than through MinIO itself.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import MinioConfig, Settings
from app.core.models import ErrorClass, PrepError, PreparedPost, PreparedImage
from app.core.sink import MinioResultSink
from app.core.source import MinioRecordSource

GROUP = "facebook/322453387859386"


class FakeStorage:
    """In-memory stand-in with the same surface MinioStorage exposes."""

    def __init__(self):
        self.objects: dict[str, str] = {}

    def get_lines(self, key):
        return [ln for ln in self.objects.get(key, "").splitlines() if ln.strip()]

    def count_lines(self, key):
        return len(self.get_lines(key))

    def iter_jsonl(self, key):
        for line in self.get_lines(key):
            yield json.loads(line)

    def list_prefixes(self, prefix):
        base = prefix.rstrip("/") + "/"
        return sorted({k[len(base):].split("/")[0] for k in self.objects if k.startswith(base)})

    def append_jsonl(self, key, rows):
        lines = self.get_lines(key) + [json.dumps(r, ensure_ascii=False) for r in rows]
        self.objects[key] = "\n".join(lines) + "\n"
        return len(lines)

    def put_jsonl(self, key, rows):
        self.objects[key] = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)

    def put_json(self, key, payload):
        self.objects[key] = json.dumps(payload)


@pytest.fixture
def settings():
    return Settings(minio=MinioConfig(group_prefix=GROUP))


@pytest.fixture
def storage():
    return FakeStorage()


def post_json(post_id, *, valid=True, urls=None, updated="2026-08-14"):
    return json.dumps({
        "post_id": post_id, "group_id": "g1", "is_valid": valid,
        "image_urls": urls if urls is not None else [f"https://cdn/{post_id}.jpg"],
        "updated_at": updated, "author": "A", "post_link": f"https://fb/{post_id}",
    })


class TestMinioPaths:
    """The output layout is a contract with the Gemini stage — pin it."""

    def test_read_paths(self, settings):
        cfg = settings.minio
        assert cfg.by_run_prefix == f"{GROUP}/logs/by_run"
        assert cfg.valid_post_key == f"{GROUP}/export/valid_post.jsonl"

    def test_write_paths(self, settings):
        cfg = settings.minio
        assert cfg.ready_for_ocr_key == f"{GROUP}/han_scan/export/ready_for_ocr.jsonl"
        assert cfg.failed_key == f"{GROUP}/han_scan/errors/failed.jsonl"
        assert cfg.processed_ids_key == f"{GROUP}/han_scan/state/processed_ids.jsonl"

    def test_per_run_paths(self, settings):
        cfg = settings.minio
        base = f"{GROUP}/han_scan/logs/by_run/S1"
        assert cfg.run_key("S1", "result.json") == f"{base}/result.json"
        assert cfg.run_key("S1", "upserts.jsonl") == f"{base}/upserts.jsonl"
        assert cfg.run_key("S1", "errors.jsonl") == f"{base}/errors.jsonl"


class TestRecordSource:
    def _seed(self, storage, cfg):
        storage.objects[f"{cfg.by_run_prefix}/20260814T092935/upserts.jsonl"] = "\n".join([
            post_json("p1"),
            post_json("p2", valid=False),           # crawler-invalid
            post_json("p3"),
            post_json("p4", urls=[]),               # no images
        ])
        storage.objects[f"{cfg.by_run_prefix}/20260815T081806/upserts.jsonl"] = "\n".join([
            post_json("p3", updated="2026-08-15"),  # re-upserted
            post_json("p5"),
        ])
        storage.objects[cfg.valid_post_key] = "\n".join(
            post_json(f"p{i}") for i in range(1, 6)
        )

    def test_claims_only_scannable_posts(self, settings, storage):
        self._seed(storage, settings.minio)
        batch = MinioRecordSource(settings, storage=storage).iter_pending(10)
        assert sorted(p.post_id for p in batch.posts) == ["p1", "p3", "p5"]

    def test_reports_progress_context(self, settings, storage):
        self._seed(storage, settings.minio)
        batch = MinioRecordSource(settings, storage=storage).iter_pending(10)
        assert batch.corpus_total == 5
        assert batch.runs_total == 2
        assert batch.current_run_id

    def test_processed_posts_are_never_reclaimed(self, settings, storage):
        self._seed(storage, settings.minio)
        source = MinioRecordSource(settings, storage=storage)
        source.mark_done(["p1", "p3"], "scan_001")
        source.invalidate_cache()

        batch = source.iter_pending(10)
        assert [p.post_id for p in batch.posts] == ["p5"]

    def test_limit_is_respected(self, settings, storage):
        self._seed(storage, settings.minio)
        batch = MinioRecordSource(settings, storage=storage).iter_pending(2)
        assert len(batch.posts) == 2

    def test_opting_in_to_crawler_invalid(self, settings, storage):
        settings = Settings(minio=settings.minio, only_crawler_valid=False)
        self._seed(storage, settings.minio)
        batch = MinioRecordSource(settings, storage=storage).iter_pending(10)
        assert "p2" in {p.post_id for p in batch.posts}

    def test_empty_store_yields_nothing(self, settings, storage):
        batch = MinioRecordSource(settings, storage=storage).iter_pending(10)
        assert batch.posts == []


class TestResultSink:
    def _record(self, post_id):
        return PreparedPost(
            post_id=post_id, group_id="g1", images_prepared=1,
            images=[PreparedImage(
                url="https://x/img", idx=0, source_url="https://cdn/x.jpg",
                sha256="abc", width=800, height=600,
            )],
        )

    def test_records_land_in_the_export_and_the_run_log(self, settings, storage):
        sink = MinioResultSink(settings, storage=storage)
        sink.write_results([self._record("p1"), self._record("p2")], "S1")

        cfg = settings.minio
        assert storage.count_lines(cfg.ready_for_ocr_key) == 2
        assert storage.count_lines(cfg.run_key("S1", "upserts.jsonl")) == 2

    def test_failures_are_written_cumulatively_and_per_run(self, settings, storage):
        sink = MinioResultSink(settings, storage=storage)
        sink.write_errors([
            PrepError(post_id="p9", group_id="g1", source_url="u",
                      error_class=ErrorClass.HTTP_404),
            PrepError(post_id="p8", group_id="g1", source_url="u",
                      error_class=ErrorClass.TIMEOUT),
        ], "S1")

        cfg = settings.minio
        assert storage.count_lines(cfg.failed_key) == 2
        assert storage.count_lines(cfg.run_key("S1", "errors.jsonl")) == 2

    def test_retryable_flag_is_serialised_for_consumers(self, settings, storage):
        sink = MinioResultSink(settings, storage=storage)
        sink.write_errors([
            PrepError(post_id="a", group_id="g", source_url="u",
                      error_class=ErrorClass.EXPIRED_URL),
            PrepError(post_id="b", group_id="g", source_url="u",
                      error_class=ErrorClass.HTTP_429),
        ], "S1")

        rows = {r["post_id"]: r for r in sink.read_failed()}
        assert rows["a"]["retryable"] is False   # dead link, never retry
        assert rows["b"]["retryable"] is True    # throttled, worth retrying
        assert [r["post_id"] for r in sink.read_failed(retryable_only=True)] == ["b"]

    def test_record_json_matches_the_documented_contract(self, settings, storage):
        record = self._record("p1").to_json()
        required = {
            "post_id", "group_id", "post_link", "author",
            "images_prepared", "images_failed", "images",
            "source_key", "source_run_id", "run_id", "stage",
            "schema_version", "prepared_at", "label", "sub_caption", "posted_at",
        }
        assert required <= set(record)
        assert record["stage"] == "han_scan"
        assert record["schema_version"] == "han_scan/2.0"
        # Schema 2.0 carries no verdict about image contents — the fields are
        # gone rather than null, so a consumer cannot read absence as "false".
        assert not {"han_valid", "scan_status", "ocr_engine"} & set(record)

    def test_prepared_image_carries_what_gemini_needs(self, settings, storage):
        image = self._record("p1").to_json()["images"][0]
        assert {"url", "idx", "source_url", "sha256", "width", "height"} <= set(image)
        assert "valid_pic" not in image

    def test_counts_summarise_the_export(self, settings, storage):
        sink = MinioResultSink(settings, storage=storage)
        sink.write_results([self._record("p1"), self._record("p2")], "S1")
        assert sink.counts() == {"ready_for_ocr": 2, "failed": 0}
