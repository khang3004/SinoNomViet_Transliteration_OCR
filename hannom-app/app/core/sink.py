"""Where verdicts go.

``ResultSink`` is the output boundary of this stage. The MinIO implementation
writes into a ``han_scan/`` namespace that mirrors the crawler's own
``export/ logs/by_run/ state/`` convention:

    <group>/han_scan/export/han_valid.jsonl          has Han text -> feeds Gemini
    <group>/han_scan/export/han_invalid.jsonl        scanned clean
    <group>/han_scan/errors/failed.jsonl             cumulative failures
    <group>/han_scan/logs/by_run/<run>/result.json   run summary
    <group>/han_scan/logs/by_run/<run>/upserts.jsonl records touched this run
    <group>/han_scan/logs/by_run/<run>/errors.jsonl  failures from this run

Failures are written twice on purpose: per-run (what broke during that run) and
cumulative (current state of every failure, for retry sweeps) — the same split
the crawler already uses between logs/by_run/ and export/.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.core.config import MinioConfig, Settings
from app.core.models import HanScanError, HanScanRecord, utcnow_iso
from app.core.storage import MinioStorage

log = logging.getLogger(__name__)


class ResultSink(Protocol):
    """Output boundary — swap this to publish verdicts somewhere else."""

    def write_results(self, records: list[HanScanRecord], scan_run_id: str) -> None: ...
    def write_errors(self, errors: list[HanScanError], scan_run_id: str) -> None: ...
    def write_run_summary(self, summary: dict[str, Any], scan_run_id: str) -> None: ...


class MinioResultSink:
    def __init__(self, settings: Settings, storage: MinioStorage | None = None) -> None:
        self.cfg: MinioConfig = settings.minio
        self.storage = storage or MinioStorage(settings.minio)

    def write_results(self, records: list[HanScanRecord], scan_run_id: str) -> None:
        """Split by verdict into the two export files, and log all of them
        under this run."""
        if not records:
            return

        valid = [r.to_json() for r in records if r.han_valid]
        invalid = [r.to_json() for r in records if not r.han_valid]

        if valid:
            self.storage.append_jsonl(self.cfg.han_valid_key, valid)
        if invalid:
            self.storage.append_jsonl(self.cfg.han_invalid_key, invalid)

        self.storage.append_jsonl(
            self.cfg.run_key(scan_run_id, "upserts.jsonl"),
            [r.to_json() for r in records],
        )
        log.info(
            "published %d records (%d han_valid, %d han_invalid) for run %s",
            len(records), len(valid), len(invalid), scan_run_id,
        )

    def write_errors(self, errors: list[HanScanError], scan_run_id: str) -> None:
        if not errors:
            return
        rows = [e.to_json() for e in errors]
        self.storage.append_jsonl(self.cfg.failed_key, rows)
        self.storage.append_jsonl(self.cfg.run_key(scan_run_id, "errors.jsonl"), rows)
        log.info("recorded %d failed images for run %s", len(errors), scan_run_id)

    def write_run_summary(self, summary: dict[str, Any], scan_run_id: str) -> None:
        summary = {**summary, "written_at": utcnow_iso()}
        self.storage.put_json(self.cfg.run_key(scan_run_id, "result.json"), summary)

    # --- read-back helpers (UI + retry sweeps) ---------------------------

    def read_failed(self, retryable_only: bool = False) -> list[dict[str, Any]]:
        rows = list(self.storage.iter_jsonl(self.cfg.failed_key))
        if retryable_only:
            rows = [r for r in rows if r.get("retryable")]
        return rows

    def counts(self) -> dict[str, int]:
        """Totals for the UI. Cheap enough at our volumes; cached by the caller."""
        return {
            "han_valid": self.storage.count_lines(self.cfg.han_valid_key),
            "han_invalid": self.storage.count_lines(self.cfg.han_invalid_key),
            "failed": self.storage.count_lines(self.cfg.failed_key),
        }
