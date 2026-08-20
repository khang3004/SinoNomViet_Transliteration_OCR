"""Record parsing, fbcdn URL expiry, filtering, and upsert dedupe."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.models import PostObject
from app.core.parser import (
    DefaultRecordParser,
    CustomRecordParser,
    decode_url_expiry,
    dedupe_posts,
    expires_within,
    expiry_iso,
    is_expired,
    is_scannable,
    parse_jsonl,
    preflight_expiry,
    to_work_items,
)

# Verified against a live crawler record: oe=6A812953 -> 2026-08-16 03:06:59 UTC
SAMPLE_URL = "https://scontent.fsgn2-3.fna.fbcdn.net/v/t39.30808-6/765853078_n.jpg?_nc_cat=107&oe=6A812953"
SAMPLE_EXPIRY = datetime(2026, 8, 16, 3, 6, 59, tzinfo=timezone.utc)


class TestUrlExpiry:
    def test_decodes_the_known_sample(self):
        assert decode_url_expiry(SAMPLE_URL) == SAMPLE_EXPIRY

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/a.jpg",          # no oe param
            "https://cdn.example.com/a.jpg?oe=ZZZZ",  # not hex
            "https://cdn.example.com/a.jpg?oe=",      # empty
            "not-even-a-url",
        ],
    )
    def test_unknown_expiry_returns_none(self, url):
        assert decode_url_expiry(url) is None

    def test_unknown_expiry_is_never_treated_as_expired(self):
        # Guessing "expired" would silently discard images that are actually fine.
        assert is_expired("https://cdn.example.com/a.jpg") is False

    def test_expired_before_and_after(self):
        assert is_expired(SAMPLE_URL, at=SAMPLE_EXPIRY - timedelta(hours=1)) is False
        assert is_expired(SAMPLE_URL, at=SAMPLE_EXPIRY + timedelta(seconds=1)) is True

    def test_expires_within_warning_band(self):
        just_before = SAMPLE_EXPIRY - timedelta(hours=3)
        assert expires_within(SAMPLE_URL, timedelta(hours=6), at=just_before) is True
        assert expires_within(SAMPLE_URL, timedelta(hours=1), at=just_before) is False

    def test_expiry_iso_roundtrips(self):
        assert expiry_iso(SAMPLE_URL) == SAMPLE_EXPIRY.isoformat()
        assert expiry_iso("https://x/a.jpg") is None


class TestParsing:
    def test_parses_the_documented_shape(self):
        raw = {
            "post_id": "p1", "group_id": "g1", "label": "L", "sub_caption": "S",
            "image_urls": [SAMPLE_URL], "image_count": 1, "is_valid": True,
            "author": "A", "post_link": "https://fb/1", "updated_at": "2026-08-15",
        }
        post = DefaultRecordParser().parse(raw)
        assert post.post_id == "p1"
        assert post.image_urls == [SAMPLE_URL]
        assert post.is_valid is True

    def test_unknown_fields_are_preserved(self):
        # A crawler schema bump must not silently drop data we pass through.
        post = DefaultRecordParser().parse(
            {"post_id": "p1", "brand_new_field": 42}
        )
        assert post.extra == {"brand_new_field": 42}

    def test_record_without_id_is_skipped(self):
        # Without an id we cannot dedupe or name output files.
        assert DefaultRecordParser().parse({"image_urls": ["u"]}) is None

    def test_falls_back_to_urls_embedded_in_images(self):
        post = DefaultRecordParser().parse(
            {"post_id": "p1", "images": [{"url": "https://x/1.jpg"}]}
        )
        assert post.image_urls == ["https://x/1.jpg"]

    def test_malformed_lines_are_counted_not_raised(self):
        lines = [
            json.dumps({"post_id": "p1"}),
            "{ not json",
            "",
            json.dumps({"post_id": "p2"}),
        ]
        posts, malformed = parse_jsonl(lines)
        assert [p.post_id for p in posts] == ["p1", "p2"]
        assert malformed == 1

    def test_custom_parser_stub_raises_clearly(self):
        with pytest.raises(NotImplementedError, match="stub"):
            CustomRecordParser().parse({"post_id": "p1"})


class TestFiltering:
    """by_run/*/upserts.jsonl holds BOTH crawler-valid and crawler-invalid posts."""

    def test_crawler_invalid_is_excluded_by_default(self):
        post = PostObject(post_id="p", is_valid=False, image_urls=["u"])
        assert is_scannable(post) is False

    def test_crawler_invalid_can_be_opted_in(self):
        post = PostObject(post_id="p", is_valid=False, image_urls=["u"])
        assert is_scannable(post, only_crawler_valid=False) is True

    def test_post_without_images_is_never_scannable(self):
        post = PostObject(post_id="p", is_valid=True, image_urls=[])
        assert is_scannable(post) is False
        assert is_scannable(post, only_crawler_valid=False) is False


class TestDedupe:
    def test_keeps_the_most_recently_updated_copy(self):
        # upserts logs repeat a post across crawl runs; without this it would be
        # rescanned once per appearance.
        posts = [
            PostObject(post_id="p1", updated_at="2026-08-14", image_urls=["old"]),
            PostObject(post_id="p1", updated_at="2026-08-15", image_urls=["new", "new2"]),
            PostObject(post_id="p2", updated_at="2026-08-14", image_urls=["x"]),
        ]
        out = {p.post_id: p for p in dedupe_posts(posts)}
        assert len(out) == 2
        assert out["p1"].image_urls == ["new", "new2"]

    def test_handles_missing_updated_at(self):
        posts = [PostObject(post_id="p1"), PostObject(post_id="p1")]
        assert len(dedupe_posts(posts)) == 1


class TestWorkItems:
    def test_every_image_becomes_a_work_item(self):
        # Judging a multi-image post by images[0] alone would drop real Han text.
        post = PostObject(post_id="p1", image_urls=[SAMPLE_URL, "https://x/2.jpg"])
        items = to_work_items(post)
        assert [i.idx for i in items] == [0, 1]
        assert items[0].source_expires_at == SAMPLE_EXPIRY.isoformat()
        assert items[1].source_expires_at is None


class TestPreflight:
    def test_buckets_by_expiry_state(self):
        now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
        dead = "https://x/a.jpg?oe=5FFFFFFF"    # 2020
        alive = "https://x/b.jpg?oe=7FFFFFFF"   # 2038
        posts = [PostObject(post_id="p", image_urls=[dead, alive, SAMPLE_URL, "https://x/n.jpg"])]

        report = preflight_expiry(posts, warn_within=timedelta(hours=6), at=now)
        assert report["total"] == 4
        assert report["expired"] == 1
        assert report["expiring_soon"] == 1   # SAMPLE expires 03:06, within 6h
        assert report["healthy"] == 1
        assert report["unknown_expiry"] == 1

    def test_empty_input_is_safe(self):
        assert preflight_expiry([])["total"] == 0
