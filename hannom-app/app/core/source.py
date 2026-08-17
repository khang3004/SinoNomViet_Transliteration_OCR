"""Where work comes from.

``RecordSource`` is the input boundary of this stage. The MinIO implementation
reads the crawler's ``logs/by_run/<run_id>/upserts.jsonl`` — chosen over the
cumulative ``export/valid_post.jsonl`` because each run directory is a
self-contained work unit needing no diffing, and the timestamped run ids give the
UI a natural "run 7 of 9" progress axis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.config import MinioConfig, Settings
from app.core.models import PostObject
from app.core.parser import (
    DefaultRecordParser,
    RecordParser,
    dedupe_posts,
    is_scannable,
    parse_jsonl,
)
from app.core.storage import MinioStorage

log = logging.getLogger(__name__)


@dataclass
class PendingBatch:
    """A claimed unit of work plus the context the UI needs to show progress."""

    posts: list[PostObject]
    source_keys: list[str]
    source_run_ids: list[str]
    malformed_lines: int = 0
    # Progress context
    corpus_total: int = 0        # lines in export/valid_post.jsonl
    processed_total: int = 0     # posts we have already scanned
    runs_total: int = 0
    runs_done: int = 0
    current_run_id: str = ""


class RecordSource(Protocol):
    """Input boundary — swap this to feed the scanner from somewhere else."""

    def iter_pending(self, limit: int) -> PendingBatch:
        """Claim up to ``limit`` unscanned posts."""
        ...

    def mark_done(self, post_ids: list[str], scan_run_id: str) -> None:
        """Record posts as scanned so they are never claimed again."""
        ...

    def processed_ids(self) -> set[str]:
        ...


class MinioRecordSource:
    """Reads crawler upsert logs from MinIO, skipping already-scanned posts."""

    def __init__(
        self,
        settings: Settings,
        storage: MinioStorage | None = None,
        parser: RecordParser | None = None,
    ) -> None:
        self.settings = settings
        self.cfg: MinioConfig = settings.minio
        self.storage = storage or MinioStorage(settings.minio)
        self.parser = parser or DefaultRecordParser()
        self._processed_cache: set[str] | None = None

    # --- idempotency -----------------------------------------------------

    def processed_ids(self) -> set[str]:
        """post_ids already scanned, from han_scan/state/processed_ids.jsonl.

        Keyed on post_id rather than run directory on purpose: the by_run logs
        are *upsert* logs, so the same post recurs across crawl runs and a
        run-keyed index would rescan it every time.
        """
        if self._processed_cache is None:
            ids: set[str] = set()
            for row in self.storage.iter_jsonl(self.cfg.processed_ids_key):
                pid = row.get("post_id")
                if pid:
                    ids.add(str(pid))
            self._processed_cache = ids
        return self._processed_cache

    def mark_done(self, post_ids: list[str], scan_run_id: str) -> None:
        if not post_ids:
            return
        from app.core.models import utcnow_iso

        rows = [
            {"post_id": pid, "scan_run_id": scan_run_id, "scanned_at": utcnow_iso()}
            for pid in post_ids
        ]
        self.storage.append_jsonl(self.cfg.processed_ids_key, rows)
        if self._processed_cache is not None:
            self._processed_cache.update(post_ids)

    def invalidate_cache(self) -> None:
        self._processed_cache = None

    # --- discovery -------------------------------------------------------

    def list_run_ids(self) -> list[str]:
        """Crawl run ids, oldest first — they are sortable timestamps."""
        return self.storage.list_prefixes(self.cfg.by_run_prefix)

    def corpus_total(self) -> int:
        """Denominator for the overall progress bar."""
        try:
            return self.storage.count_lines(self.cfg.valid_post_key)
        except Exception as exc:  # noqa: BLE001 - a missing denominator is cosmetic
            log.warning("could not read corpus total: %s", exc)
            return 0

    def iter_pending(self, limit: int) -> PendingBatch:
        """Walk run directories oldest-first, collecting unscanned posts.

        Stops as soon as ``limit`` posts are gathered, so a run with 10k posts
        is consumed across several batches rather than in one bite.
        """
        processed = self.processed_ids()
        run_ids = self.list_run_ids()

        posts: list[PostObject] = []
        source_keys: list[str] = []
        source_run_ids: list[str] = []
        malformed = 0
        runs_done = 0
        current_run = ""

        for run_id in run_ids:
            if len(posts) >= limit:
                break

            key = f"{self.cfg.by_run_prefix}/{run_id}/upserts.jsonl"
            lines = self.storage.get_lines(key)
            if not lines:
                runs_done += 1
                continue

            parsed, bad = parse_jsonl(lines, self.parser)
            malformed += bad

            # by_run holds BOTH crawler-valid and crawler-invalid posts.
            candidates = [
                p for p in parsed
                if is_scannable(p, only_crawler_valid=self.settings.only_crawler_valid)
            ]
            candidates = dedupe_posts(candidates)
            fresh = [p for p in candidates if p.post_id not in processed]

            if not fresh:
                runs_done += 1
                continue

            current_run = run_id
            take = fresh[: limit - len(posts)]
            posts.extend(take)
            source_keys.append(key)
            source_run_ids.append(run_id)

            if len(take) == len(fresh):
                runs_done += 1

        # A post may appear in several run logs; keep one copy.
        posts = dedupe_posts(posts)

        return PendingBatch(
            posts=posts,
            source_keys=source_keys,
            source_run_ids=source_run_ids,
            malformed_lines=malformed,
            corpus_total=self.corpus_total(),
            processed_total=len(processed),
            runs_total=len(run_ids),
            runs_done=runs_done,
            current_run_id=current_run,
        )

    def source_key_for(self, run_id: str) -> str:
        return f"{self.cfg.by_run_prefix}/{run_id}/upserts.jsonl"
