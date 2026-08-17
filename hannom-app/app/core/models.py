"""Data contracts for the han_scan stage.

Input comes from the crawler (``PostObject``); output is consumed by the Gemini
batch stage (``HanScanRecord``). Both are serialised as JSONL in MinIO, so the
field names here ARE the wire format — renaming one is a breaking change for the
next stage.

Naming note: the crawler's ``is_valid`` means "the crawl succeeded and matched".
Ours is a different question entirely (does the image contain Han text), so it is
``han_valid``. Overloading ``is_valid`` would silently corrupt meaning downstream.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = "han_scan/1.0"
STAGE = "han_scan"

# CJK Unified Ideographs, Extension A, Compatibility Ideographs, Extension B.
# Deliberately excludes kana and Hangul — those are not Han.
CJK_PATTERN = re.compile(
    r"[一-鿿㐀-䶿豈-﫿\U00020000-\U0002a6df]"
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_han_chars(text: str) -> int:
    """Number of Han characters in a string."""
    return len(CJK_PATTERN.findall(text))


class ErrorClass(str, Enum):
    """Why an image could not be scanned.

    ``retryable`` distinguishes transient failures from permanently dead links,
    so a retry sweep does not hammer URLs that will never come back.
    """

    EXPIRED_URL = "expired_url"
    HTTP_403 = "http_403"
    HTTP_404 = "http_404"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    DECODE_ERROR = "decode_error"
    OCR_ERROR = "ocr_error"
    TOO_LARGE = "too_large"

    @property
    def retryable(self) -> bool:
        return self in {
            ErrorClass.HTTP_429,
            ErrorClass.HTTP_5XX,
            ErrorClass.TIMEOUT,
            ErrorClass.DNS_ERROR,
            ErrorClass.OCR_ERROR,
        }


# --------------------------------------------------------------------------
# Input side — mirrors the crawler's valid_post.jsonl / upserts.jsonl records
# --------------------------------------------------------------------------


@dataclass
class ImageMetadata:
    """Crawler-side image metadata. Arrives all-null; we populate it."""

    url: str | None = None
    width: int | None = None
    height: int | None = None
    alt_text: str | None = None


@dataclass
class PostObject:
    """One crawled post. Unknown fields are preserved in ``extra`` so a crawler
    schema bump does not silently drop data we pass through."""

    post_id: str = ""
    group_id: str = ""
    label: str = ""
    sub_caption: str = ""
    sub_captions: list[str] = field(default_factory=list)
    images: list[ImageMetadata] = field(default_factory=list)
    story_post_id: str | None = None
    tile_id: str | None = None
    tile_ids: list[str] = field(default_factory=list)
    post_link: str = ""
    author: str = ""
    image_urls: list[str] = field(default_factory=list)
    image_count: int = 0
    posted_at: str | None = None
    images_downloaded: bool = False
    images_download_skipped: bool = False
    is_valid: bool = False
    invalid_reason: str | None = None
    match_status: str = ""
    phase: str = ""
    schema_version: str = ""
    source: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkItem:
    """One image to fetch and scan — the unit the batch pipeline moves through
    its phases. Flattened from PostObject because a post with 3 images is 3
    independent units of work that can fail independently."""

    post_id: str
    idx: int
    source_url: str
    source_expires_at: str | None = None
    local_path: str | None = None


# --------------------------------------------------------------------------
# Output side — the contract the Gemini batch stage reads
# --------------------------------------------------------------------------


@dataclass
class ScannedImage:
    url: str  # our domain, HMAC-signed — Gemini fetches this
    idx: int
    width: int | None = None
    height: int | None = None
    bytes: int | None = None
    content_type: str | None = None
    sha256: str | None = None
    source_url: str = ""
    source_expires_at: str | None = None
    url_expires_at: str = ""
    downloaded_at: str = ""

    valid_pic: bool = False
    han_words: int = 0
    boxes: int = 0
    texts: list[str] = field(default_factory=list)
    mean_confidence: float | None = None
    scan_ms: int = 0


@dataclass
class HanScanRecord:
    post_id: str
    group_id: str
    post_link: str = ""
    author: str = ""
    story_post_id: str | None = None
    tile_id: str | None = None

    han_valid: bool = False
    han_words_total: int = 0
    images_scanned: int = 0
    images_failed: int = 0
    images: list[ScannedImage] = field(default_factory=list)

    source_key: str = ""
    source_run_id: str = ""
    scan_run_id: str = ""
    stage: str = STAGE
    schema_version: str = SCHEMA_VERSION
    ocr_engine: str = ""
    scanned_at: str = field(default_factory=utcnow_iso)

    label: str = ""
    sub_caption: str = ""
    posted_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HanScanError:
    post_id: str
    group_id: str
    source_url: str
    error_class: ErrorClass
    error_detail: str = ""
    source_expires_at: str | None = None
    http_status: int | None = None
    attempts: int = 0
    idx: int = 0
    first_seen_at: str = field(default_factory=utcnow_iso)
    last_attempt_at: str = field(default_factory=utcnow_iso)
    scan_run_id: str = ""

    @property
    def retryable(self) -> bool:
        return self.error_class.retryable

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["error_class"] = self.error_class.value
        # Derived, but written explicitly so consumers need no enum knowledge.
        data["retryable"] = self.retryable
        return data


@dataclass
class ScanOutcome:
    """Result of OCR on a single image. Crosses a process boundary (the OCR
    pool), so it stays plain data with no open handles."""

    idx: int
    post_id: str
    ok: bool
    valid_pic: bool = False
    han_words: int = 0
    boxes: int = 0
    texts: list[str] = field(default_factory=list)
    mean_confidence: float | None = None
    scan_ms: int = 0
    error_class: ErrorClass | None = None
    error_detail: str = ""
