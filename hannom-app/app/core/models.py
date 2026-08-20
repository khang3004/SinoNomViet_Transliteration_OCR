"""Data contracts for the image-preparation stage.

Input comes from the crawler (``PostObject``); output is consumed by the Gemini
batch stage (``PreparedPost``). Both are serialised as JSONL, so the field names
here ARE the wire format — renaming one breaks the next stage.

This stage downloads images and makes them fetchable from our domain. It makes
no claim about their contents: schema 2.0 dropped the Han-detection verdict
fields rather than leaving them permanently null, because a null field that can
never be filled invites a consumer to read it as "false".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = "han_scan/2.0"
STAGE = "han_scan"

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ErrorClass(str, Enum):
    """Why an image could not be prepared.

    ``retryable`` distinguishes transient failures from permanently dead links,
    so a retry sweep does not hammer URLs that will never come back.

    DECODE_ERROR and OCR_ERROR are no longer produced (that stage was removed)
    but stay defined: ``failed.jsonl`` is cumulative, and the retry sweep parses
    error classes out of rows written before the change.
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
    """One image to fetch — the unit the batch pipeline moves through its
    phases. Flattened from PostObject because a post with 3 images is 3
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
class PreparedImage:
    """One downloaded image, addressable from our domain.

    Carries no verdict about its contents — this stage does not look inside
    images, it makes them fetchable.
    """

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


@dataclass
class PreparedPost:
    post_id: str
    group_id: str
    post_link: str = ""
    author: str = ""
    story_post_id: str | None = None
    tile_id: str | None = None

    images_prepared: int = 0
    images_failed: int = 0
    images: list[PreparedImage] = field(default_factory=list)

    source_key: str = ""
    source_run_id: str = ""
    run_id: str = ""
    stage: str = STAGE
    schema_version: str = SCHEMA_VERSION
    prepared_at: str = field(default_factory=utcnow_iso)

    label: str = ""
    sub_caption: str = ""
    posted_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrepError:
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
    run_id: str = ""

    @property
    def retryable(self) -> bool:
        return self.error_class.retryable

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["error_class"] = self.error_class.value
        # Derived, but written explicitly so consumers need no enum knowledge.
        data["retryable"] = self.retryable
        return data
