"""Local processed-post checkpoint.

``valid_post.jsonl`` is the crawler's **cumulative** export — every upload
contains every post seen so far, not just the new ones. Re-uploading it after a
fresh crawl is therefore the normal workflow, and without a checkpoint every
upload would rescan the entire corpus.

Keyed on ``post_id`` rather than on the upload, for the same reason the MinIO
path is: the same post recurs across exports.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from app.core.models import utcnow_iso

log = logging.getLogger(__name__)


class ProcessedCheckpoint:
    """Append-only record of post_ids already scanned."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: set[str] | None = None

    def load(self) -> set[str]:
        if self._cache is not None:
            return self._cache

        ids: set[str] = set()
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        post_id = json.loads(line).get("post_id")
                    except json.JSONDecodeError:
                        # Torn final line from a hard kill; earlier ones stand.
                        continue
                    if post_id:
                        ids.add(str(post_id))
        self._cache = ids
        return ids

    def contains(self, post_id: str) -> bool:
        return post_id in self.load()

    def add(self, post_ids: Iterable[str], scan_run_id: str) -> int:
        post_ids = [pid for pid in post_ids if pid]
        if not post_ids:
            return 0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = utcnow_iso()
        with open(self.path, "a", encoding="utf-8") as handle:
            for post_id in post_ids:
                handle.write(
                    json.dumps(
                        {"post_id": post_id, "scan_run_id": scan_run_id, "scanned_at": stamp},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())

        if self._cache is not None:
            self._cache.update(post_ids)
        return len(post_ids)

    def remove(self, post_ids: Iterable[str]) -> int:
        """Drop ids so they can be reclaimed — used by the retry-failed sweep.

        Rewrites the file, which is fine at checkpoint sizes and only happens on
        an explicit retry.
        """
        drop = {pid for pid in post_ids if pid}
        if not drop or not self.path.exists():
            return 0

        kept: list[str] = []
        removed = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if str(json.loads(line).get("post_id")) in drop:
                        removed += 1
                        continue
                except json.JSONDecodeError:
                    continue
                kept.append(line)

        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(self.path)
        self.invalidate()
        return removed

    def invalidate(self) -> None:
        self._cache = None

    def count(self) -> int:
        return len(self.load())
