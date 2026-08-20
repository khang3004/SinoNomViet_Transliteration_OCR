"""The admin gallery: paging, search, and signed-URL re-minting."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.gallery import Gallery
from app.core.signing import ImageUrlSigner, sign, verify

SECRET = "gallery-secret"


def record(post_id, *, author="A", label="", images=1, exp=None):
    """One export line. ``exp`` forces a stored signature expiry."""
    expires = exp if exp is not None else int(time.time()) + 86400
    return {
        "post_id": post_id, "group_id": "g1", "author": author,
        "post_link": f"https://facebook.com/{post_id}", "label": label,
        "sub_caption": "", "posted_at": None, "prepared_at": "2026-08-17T00:00:00+00:00",
        "run_id": "R1", "images_prepared": images, "images_failed": 0,
        "stage": "han_scan", "schema_version": "han_scan/2.0",
        "images": [
            {
                "url": f"https://x/img/{post_id}/{i}.jpg?exp={expires}"
                       f"&sig={sign(post_id, i, expires, SECRET)}",
                "idx": i, "width": 800, "height": 600, "bytes": 1234,
                "content_type": "image/jpeg", "sha256": "abc",
                "source_url": f"https://cdn/{post_id}_{i}.jpg",
                "source_expires_at": "2026-08-18T00:00:00+00:00",
                "url_expires_at": "2026-09-16T00:00:00+00:00",
                "downloaded_at": "2026-08-17T00:00:00+00:00",
            }
            for i in range(images)
        ],
    }


@pytest.fixture
def export(tmp_path):
    def write(*records):
        path = tmp_path / "ready_for_ocr.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        return path

    return write


@pytest.fixture
def signer():
    return ImageUrlSigner("https://scan.example.com", SECRET, ttl_days=30)


class TestPaging:
    def test_missing_export_is_survivable(self, tmp_path, signer):
        gallery = Gallery(tmp_path / "nope.jsonl", signer)
        page = gallery.page()
        assert page.items == [] and page.total == 0

    def test_newest_first(self, export, signer):
        # The export is append-only, so the last line is the most recent work.
        gallery = Gallery(export(record("p1"), record("p2"), record("p3")), signer)
        assert [i["post_id"] for i in gallery.page().items] == ["p3", "p2", "p1"]

    def test_offset_and_limit(self, export, signer):
        gallery = Gallery(export(*[record(f"p{i}") for i in range(10)]), signer)

        first = gallery.page(offset=0, limit=4)
        assert len(first.items) == 4 and first.total == 10 and first.to_json()["has_more"]

        last = gallery.page(offset=8, limit=4)
        assert len(last.items) == 2 and last.to_json()["has_more"] is False

    def test_offset_past_the_end_is_empty_not_an_error(self, export, signer):
        gallery = Gallery(export(record("p1")), signer)
        assert gallery.page(offset=500).items == []

    def test_malformed_line_does_not_break_the_page(self, tmp_path, signer):
        path = tmp_path / "ready_for_ocr.jsonl"
        path.write_text(
            json.dumps(record("p1")) + "\n{ torn line",
            encoding="utf-8",
        )
        assert Gallery(path, signer).page().total == 1


class TestSearch:
    def test_by_author(self, export, signer):
        gallery = Gallery(
            export(record("p1", author="董惠珍"), record("p2", author="Someone")), signer
        )
        assert [i["post_id"] for i in gallery.page(query="董惠珍").items] == ["p1"]

    def test_by_post_id(self, export, signer):
        gallery = Gallery(export(record("abc123"), record("xyz789")), signer)
        assert [i["post_id"] for i in gallery.page(query="abc").items] == ["abc123"]

    def test_by_caption_text(self, export, signer):
        gallery = Gallery(
            export(record("p1", label="年歲漸長"), record("p2", label="other")), signer
        )
        assert [i["post_id"] for i in gallery.page(query="年歲").items] == ["p1"]

    def test_is_case_insensitive(self, export, signer):
        gallery = Gallery(export(record("p1", author="Alice")), signer)
        assert gallery.page(query="alice").total == 1

    def test_no_match_is_empty(self, export, signer):
        gallery = Gallery(export(record("p1")), signer)
        assert gallery.page(query="nothing-like-this").total == 0

    def test_total_reflects_the_filter_not_the_file(self, export, signer):
        gallery = Gallery(
            export(record("p1", author="Nguyen"), record("p2", author="Tran")), signer
        )
        assert gallery.page(query="nguyen").total == 1

    def test_search_spans_the_post_link(self, export, signer):
        # Pasting a Facebook URL should find the post. The flip side is that a
        # very short query matches broadly, since every link shares a domain.
        gallery = Gallery(export(record("abc123"), record("xyz789")), signer)
        assert gallery.page(query="facebook.com/abc123").total == 1
        assert gallery.page(query="facebook").total == 2


class TestSignedUrlReminting:
    def test_served_url_is_freshly_signed(self, export, signer):
        gallery = Gallery(export(record("p1")), signer)
        url = gallery.page().items[0]["images"][0]["url"]

        params = parse_qs(urlparse(url).query)
        assert verify("p1", 0, int(params["exp"][0]), params["sig"][0], SECRET)

    def test_expired_stored_signature_is_replaced(self, export, signer):
        # Records written a month ago carry dead signatures; browsing them must
        # still show thumbnails.
        stale = int(time.time()) - 86400
        gallery = Gallery(export(record("p1", exp=stale)), signer)
        item = gallery.page().items[0]["images"][0]

        params = parse_qs(urlparse(item["url"]).query)
        assert int(params["exp"][0]) > time.time()
        assert verify("p1", 0, int(params["exp"][0]), params["sig"][0], SECRET)
        # The original is preserved for reference.
        assert f"exp={stale}" in item["stored_url"]

    def test_without_a_signer_the_stored_url_is_used(self, export):
        gallery = Gallery(export(record("p1")), signer=None)
        item = gallery.page().items[0]["images"][0]
        assert item["url"] == item["stored_url"]

    def test_unconfigured_secret_falls_back_rather_than_failing(self, export):
        # An empty secret makes minting raise; a broken page is worse than a
        # stale link.
        gallery = Gallery(export(record("p1")), ImageUrlSigner("https://x", "", 30))
        assert gallery.page().items[0]["images"][0]["url"]


class TestPresentation:
    def test_card_carries_the_post_context(self, export, signer):
        gallery = Gallery(export(record("p1", author="董惠珍", label="caption")), signer)
        item = gallery.page().items[0]

        # "which image, and in what post" is the whole point of the view.
        assert item["author"] == "董惠珍"
        assert item["label"] == "caption"
        assert item["post_link"].endswith("p1")
        assert item["images_prepared"] == 1

    def test_image_metadata_is_exposed_for_the_lightbox(self, export, signer):
        gallery = Gallery(export(record("p1")), signer)
        image = gallery.page().items[0]["images"][0]

        for field in ("width", "height", "bytes", "sha256", "source_url",
                      "source_expires_at", "url_expires_at", "downloaded_at"):
            assert field in image

    def test_multi_image_posts_expose_every_image(self, export, signer):
        gallery = Gallery(export(record("p1", images=3)), signer)
        assert [i["idx"] for i in gallery.page().items[0]["images"]] == [0, 1, 2]

    def test_stats(self, export, signer):
        gallery = Gallery(
            export(record("p1", author="A", images=2), record("p2", author="B")), signer
        )
        assert gallery.stats() == {"posts": 2, "images": 3, "authors": 2}


class TestLookupAndCache:
    def test_get_one_post(self, export, signer):
        gallery = Gallery(export(record("p1"), record("p2")), signer)
        assert gallery.get("p2")["post_id"] == "p2"
        assert gallery.get("nope") is None

    def test_cache_refreshes_when_the_export_grows(self, tmp_path, signer):
        path = tmp_path / "ready_for_ocr.jsonl"
        path.write_text(json.dumps(record("p1")) + "\n", encoding="utf-8")
        gallery = Gallery(path, signer)
        assert gallery.page().total == 1

        # mtime resolution is coarse on some filesystems; force a distinct stamp.
        time.sleep(0.01)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("p2")) + "\n")
        import os
        os.utime(path, (time.time() + 1, time.time() + 1))

        assert gallery.page().total == 2

    def test_invalidate_forces_a_reread(self, export, signer):
        gallery = Gallery(export(record("p1")), signer)
        gallery.page()
        gallery.invalidate()
        assert gallery.page().total == 1
