"""Browsable view over the prepared-post export.

Backs the admin gallery: which images have been prepared, and which post each
came from. Read-only — nothing here mutates the export.

Two decisions worth knowing:

* **Signed URLs are re-minted on read.** The URL stored in a record was signed
  when the record was written and expires after ``IMAGE_URL_TTL_DAYS``. Serving
  the stored URL would mean thumbnails silently breaking a month later, so the
  gallery signs fresh ones. The stored URL is still what the Gemini stage
  consumes; this only affects browsing.

* **The export is cached in memory, invalidated on mtime.** At 20k records that
  is roughly 20 MB, which buys instant paging and search. Re-reading a 40 MB
  JSONL on every poll would not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.signing import ImageUrlSigner, SigningError

log = logging.getLogger(__name__)


@dataclass
class GalleryPage:
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    query: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "query": self.query,
            "has_more": self.offset + len(self.items) < self.total,
        }


class Gallery:
    def __init__(self, export_path: Path, signer: ImageUrlSigner | None = None) -> None:
        self.export_path = Path(export_path)
        self.signer = signer
        self._cache: list[dict[str, Any]] | None = None
        self._cached_mtime: float | None = None

    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        """Records newest-first, cached until the file changes."""
        if not self.export_path.exists():
            self._cache, self._cached_mtime = [], None
            return []

        mtime = self.export_path.stat().st_mtime
        if self._cache is not None and self._cached_mtime == mtime:
            return self._cache

        rows: list[dict[str, Any]] = []
        with open(self.export_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn final line from a crash; the rest still stands.
                    continue

        # The export is append-only, so reversing puts the most recent work
        # first — which is what someone opening the page wants to see.
        rows.reverse()
        self._cache, self._cached_mtime = rows, mtime
        log.info("gallery cache: %d records from %s", len(rows), self.export_path.name)
        return rows

    def invalidate(self) -> None:
        self._cache = None
        self._cached_mtime = None

    # ------------------------------------------------------------------

    @staticmethod
    def _matches(record: dict[str, Any], needle: str) -> bool:
        if not needle:
            return True
        haystack = " ".join(
            str(record.get(field, ""))
            for field in ("post_id", "author", "label", "sub_caption", "post_link")
        ).lower()
        return needle in haystack

    def _sign(self, post_id: str, image: dict[str, Any]) -> str:
        """Fresh signature so the thumbnail always loads."""
        stored = image.get("url", "")
        if self.signer is None:
            return stored
        suffix = Path(str(stored).split("?")[0]).suffix or ".jpg"
        try:
            url, _ = self.signer.build(post_id, int(image.get("idx", 0)), suffix)
            return url
        except (SigningError, ValueError, TypeError):
            # No secret configured, or a malformed record — fall back rather
            # than failing the whole page.
            return stored

    def _present(self, record: dict[str, Any]) -> dict[str, Any]:
        """One card's worth of data: the post, and its images."""
        post_id = str(record.get("post_id", ""))
        images = record.get("images") or []
        return {
            "post_id": post_id,
            "group_id": record.get("group_id", ""),
            "author": record.get("author", ""),
            "post_link": record.get("post_link", ""),
            "label": record.get("label", ""),
            "sub_caption": record.get("sub_caption", ""),
            "posted_at": record.get("posted_at"),
            "prepared_at": record.get("prepared_at", ""),
            "run_id": record.get("run_id", ""),
            "images_prepared": record.get("images_prepared", len(images)),
            "images_failed": record.get("images_failed", 0),
            "images": [
                {
                    "url": self._sign(post_id, image),
                    "stored_url": image.get("url", ""),
                    "idx": image.get("idx", 0),
                    "width": image.get("width"),
                    "height": image.get("height"),
                    "bytes": image.get("bytes"),
                    "content_type": image.get("content_type"),
                    "sha256": image.get("sha256"),
                    "source_url": image.get("source_url", ""),
                    "source_expires_at": image.get("source_expires_at"),
                    "url_expires_at": image.get("url_expires_at"),
                    "downloaded_at": image.get("downloaded_at"),
                }
                for image in images
            ],
        }

    def page(self, offset: int = 0, limit: int = 24, query: str = "") -> GalleryPage:
        needle = query.strip().lower()
        rows = self._load()
        if needle:
            rows = [r for r in rows if self._matches(r, needle)]

        window = rows[offset : offset + limit]
        return GalleryPage(
            items=[self._present(r) for r in window],
            total=len(rows), offset=offset, limit=limit, query=query,
        )

    def get(self, post_id: str) -> dict[str, Any] | None:
        for record in self._load():
            if str(record.get("post_id")) == post_id:
                return self._present(record)
        return None

    def stats(self) -> dict[str, Any]:
        rows = self._load()
        return {
            "posts": len(rows),
            "images": sum(len(r.get("images") or []) for r in rows),
            "authors": len({r.get("author", "") for r in rows if r.get("author")}),
        }
