"""Parsing crawler records into work items, plus fbcdn URL expiry decoding.

The exact shape of the MinIO input is not fully pinned down yet, so this module
exposes a ``RecordParser`` protocol with a permissive default implementation.
Swap in your own parser without touching anything else in the pipeline — see
``CustomRecordParser`` at the bottom.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol
from urllib.parse import parse_qs, urlparse

from app.core.models import ImageMetadata, PostObject, WorkItem

log = logging.getLogger(__name__)

# Fields we map explicitly; anything else lands in PostObject.extra.
_KNOWN_FIELDS = {
    "post_id", "group_id", "label", "sub_caption", "sub_captions", "images",
    "story_post_id", "tile_id", "tile_ids", "post_link", "author", "image_urls",
    "image_count", "posted_at", "images_downloaded", "images_download_skipped",
    "is_valid", "invalid_reason", "match_status", "phase", "schema_version",
    "source", "updated_at",
}


# --------------------------------------------------------------------------
# fbcdn signed-URL expiry
# --------------------------------------------------------------------------


def decode_url_expiry(url: str) -> datetime | None:
    """Decode the fbcdn ``oe`` parameter into an expiry datetime.

    fbcdn signs image URLs with a hex unix timestamp in ``oe``. Verified against
    a live sample: ``oe=6A812953`` -> 2026-08-16 03:06:59 UTC.

    Returns None when the parameter is absent or unparseable — callers treat
    "unknown expiry" as "assume fresh", since guessing expired would drop images
    that are actually fine.
    """
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:  # noqa: BLE001 - a malformed URL is not an expiry answer
        return None

    raw = (params.get("oe") or [None])[0]
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw, 16), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        # Not hex, or a timestamp outside the representable range.
        return None


def is_expired(url: str, *, at: datetime | None = None) -> bool:
    """True only when we can prove the URL has expired."""
    expiry = decode_url_expiry(url)
    if expiry is None:
        return False
    return expiry <= (at or datetime.now(timezone.utc))


def expires_within(url: str, delta: timedelta, *, at: datetime | None = None) -> bool:
    """True when the URL dies inside ``delta`` — the preflight warning band."""
    expiry = decode_url_expiry(url)
    if expiry is None:
        return False
    return expiry <= (at or datetime.now(timezone.utc)) + delta


def expiry_iso(url: str) -> str | None:
    expiry = decode_url_expiry(url)
    return expiry.isoformat() if expiry else None


# --------------------------------------------------------------------------
# Record parsing
# --------------------------------------------------------------------------


def _coerce_images(raw: Any) -> list[ImageMetadata]:
    if not isinstance(raw, list):
        return []
    out: list[ImageMetadata] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                ImageMetadata(
                    url=item.get("url"),
                    width=item.get("width"),
                    height=item.get("height"),
                    alt_text=item.get("alt_text"),
                )
            )
        elif isinstance(item, str):
            # Some crawler versions emit bare URL strings here.
            out.append(ImageMetadata(url=item))
    return out


def _coerce_str_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


class RecordParser(Protocol):
    """Turns one raw JSON object from MinIO into a ``PostObject``.

    Implement this to handle a crawler schema this module does not know about.
    """

    def parse(self, raw: dict[str, Any]) -> PostObject | None:
        """Return the parsed post, or None to skip the record entirely."""
        ...


class DefaultRecordParser:
    """Permissive parser for the known crawler shape.

    Tolerates missing/renamed optional fields and preserves unrecognised keys in
    ``PostObject.extra`` so a crawler schema bump does not silently drop data.
    """

    def parse(self, raw: dict[str, Any]) -> PostObject | None:
        if not isinstance(raw, dict):
            return None

        post_id = str(raw.get("post_id") or raw.get("id") or "").strip()
        if not post_id:
            # Without an id we cannot dedupe or name output files.
            return None

        image_urls = _coerce_str_list(raw.get("image_urls"))
        images = _coerce_images(raw.get("images"))
        # Fall back to urls embedded in `images` when `image_urls` is absent.
        if not image_urls and images:
            image_urls = [i.url for i in images if i.url]

        return PostObject(
            post_id=post_id,
            group_id=str(raw.get("group_id") or ""),
            label=str(raw.get("label") or ""),
            sub_caption=str(raw.get("sub_caption") or ""),
            sub_captions=_coerce_str_list(raw.get("sub_captions")),
            images=images,
            story_post_id=raw.get("story_post_id"),
            tile_id=raw.get("tile_id"),
            tile_ids=_coerce_str_list(raw.get("tile_ids")),
            post_link=str(raw.get("post_link") or ""),
            author=str(raw.get("author") or ""),
            image_urls=image_urls,
            image_count=int(raw.get("image_count") or len(image_urls)),
            posted_at=raw.get("posted_at"),
            images_downloaded=bool(raw.get("images_downloaded")),
            images_download_skipped=bool(raw.get("images_download_skipped")),
            is_valid=bool(raw.get("is_valid")),
            invalid_reason=raw.get("invalid_reason"),
            match_status=str(raw.get("match_status") or ""),
            phase=str(raw.get("phase") or ""),
            schema_version=str(raw.get("schema_version") or ""),
            source=str(raw.get("source") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            extra={k: v for k, v in raw.items() if k not in _KNOWN_FIELDS},
        )


class CustomRecordParser:
    """TODO(hannom): fill in once the real MinIO record shape is confirmed.

    Wire it up by passing an instance to ``parse_jsonl`` / the MinIO source
    instead of ``DefaultRecordParser()``. Nothing else in the pipeline changes —
    that is the point of the ``RecordParser`` protocol.

    Example::

        def parse(self, raw):
            return PostObject(
                post_id=raw["identifier"],
                image_urls=[m["src"] for m in raw["media"]],
                is_valid=raw["status"] == "ok",
                ...
            )
    """

    def parse(self, raw: dict[str, Any]) -> PostObject | None:
        raise NotImplementedError(
            "CustomRecordParser is a stub. Implement parse() for your record "
            "shape, or use DefaultRecordParser for the standard crawler schema."
        )


# --------------------------------------------------------------------------
# JSONL -> posts -> work items
# --------------------------------------------------------------------------


def parse_jsonl(
    lines: Iterable[str | bytes],
    parser: RecordParser | None = None,
) -> tuple[list[PostObject], int]:
    """Parse JSONL into posts. Returns (posts, malformed_line_count).

    Malformed lines are counted and skipped rather than raising — one bad line
    in an upserts log must not cost the whole batch.
    """
    parser = parser or DefaultRecordParser()
    posts: list[PostObject] = []
    malformed = 0

    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        try:
            post = parser.parse(raw)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad record, not the batch
            log.warning("parser rejected a record: %s", exc)
            malformed += 1
            continue
        if post is not None:
            posts.append(post)

    return posts, malformed


def is_scannable(post: PostObject, *, only_crawler_valid: bool = True) -> bool:
    """Whether this post is worth downloading and scanning.

    ``by_run/*/upserts.jsonl`` holds both crawler-valid and crawler-invalid
    posts, so this filter is what keeps the invalid ones out.
    """
    if only_crawler_valid and not post.is_valid:
        return False
    return bool(post.image_urls)


def dedupe_posts(posts: Iterable[PostObject]) -> list[PostObject]:
    """Collapse repeated post_ids, keeping the most recently updated.

    The by_run logs are *upsert* logs, so the same post recurs across crawl runs.
    Without this, a re-crawled post would be scanned once per appearance.
    """
    latest: dict[str, PostObject] = {}
    for post in posts:
        prior = latest.get(post.post_id)
        if prior is None or (post.updated_at or "") >= (prior.updated_at or ""):
            latest[post.post_id] = post
    return list(latest.values())


def to_work_items(post: PostObject) -> list[WorkItem]:
    """Flatten a post into one work item per image.

    Every image is scanned, not just the first — a post whose second image
    carries the Han text would otherwise be discarded.
    """
    return [
        WorkItem(
            post_id=post.post_id,
            idx=idx,
            source_url=url,
            source_expires_at=expiry_iso(url),
        )
        for idx, url in enumerate(post.image_urls)
    ]


def preflight_expiry(
    posts: Iterable[PostObject],
    *,
    warn_within: timedelta = timedelta(hours=6),
    at: datetime | None = None,
) -> dict[str, Any]:
    """Expiry audit run before any downloading.

    Cheap (pure string parsing) and the single most useful guard in the pipeline:
    it distinguishes "the crawl is fresh, proceed" from "these links are already
    dead, re-crawl instead of burning hours".
    """
    now = at or datetime.now(timezone.utc)
    total = expired = expiring = healthy = unknown = 0
    earliest: datetime | None = None

    for post in posts:
        for url in post.image_urls:
            total += 1
            expiry = decode_url_expiry(url)
            if expiry is None:
                unknown += 1
                continue
            if earliest is None or expiry < earliest:
                earliest = expiry
            if expiry <= now:
                expired += 1
            elif expiry <= now + warn_within:
                expiring += 1
            else:
                healthy += 1

    return {
        "total": total,
        "expired": expired,
        "expiring_soon": expiring,
        "healthy": healthy,
        "unknown_expiry": unknown,
        "earliest_expiry": earliest.isoformat() if earliest else None,
        "warn_within_hours": warn_within.total_seconds() / 3600,
        "checked_at": now.isoformat(),
    }
