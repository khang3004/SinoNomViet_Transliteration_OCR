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

import json
import logging
import os
from pathlib import Path
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


class FileResultSink:
    """Writes verdicts to local disk for download from the dashboard.

    Mirrors the MinIO layout exactly, so a file produced here and one produced
    there are interchangeable to the Gemini stage::

        <data>/results/export/han_valid.jsonl
        <data>/results/export/han_invalid.jsonl
        <data>/results/errors/failed.jsonl
        <data>/results/logs/by_run/<run>/{result.json,upserts.jsonl,errors.jsonl}
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- paths ----------------------------------------------------------

    @property
    def han_valid_path(self) -> Path:
        return self.root / "export" / "han_valid.jsonl"

    @property
    def han_invalid_path(self) -> Path:
        return self.root / "export" / "han_invalid.jsonl"

    @property
    def failed_path(self) -> Path:
        return self.root / "errors" / "failed.jsonl"

    def run_path(self, scan_run_id: str, name: str) -> Path:
        return self.root / "logs" / "by_run" / scan_run_id / name

    # --- writes ---------------------------------------------------------

    @staticmethod
    def _append(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            # Results are the whole point of a multi-hour run; do not leave them
            # sitting in a page cache that a crash would drop.
            os.fsync(handle.fileno())

    def write_results(self, records: list[HanScanRecord], scan_run_id: str) -> None:
        if not records:
            return
        rows = [r.to_json() for r in records]
        valid = [r for r, rec in zip(rows, records) if rec.han_valid]
        invalid = [r for r, rec in zip(rows, records) if not rec.han_valid]

        self._append(self.han_valid_path, valid)
        self._append(self.han_invalid_path, invalid)
        self._append(self.run_path(scan_run_id, "upserts.jsonl"), rows)
        log.info(
            "wrote %d record(s) locally (%d han_valid, %d han_invalid)",
            len(rows), len(valid), len(invalid),
        )

    def write_errors(self, errors: list[HanScanError], scan_run_id: str) -> None:
        if not errors:
            return
        rows = [e.to_json() for e in errors]
        self._append(self.failed_path, rows)
        self._append(self.run_path(scan_run_id, "errors.jsonl"), rows)

    def write_run_summary(self, summary: dict[str, Any], scan_run_id: str) -> None:
        path = self.run_path(scan_run_id, "result.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({**summary, "written_at": utcnow_iso()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --- read-back (UI + retry sweeps) ----------------------------------

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    @staticmethod
    def _count(path: Path) -> int:
        if not path.exists():
            return 0
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def read_failed(self, retryable_only: bool = False) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self.failed_path)
        return [r for r in rows if r.get("retryable")] if retryable_only else rows

    def counts(self) -> dict[str, int]:
        return {
            "han_valid": self._count(self.han_valid_path),
            "han_invalid": self._count(self.han_invalid_path),
            "failed": self._count(self.failed_path),
        }

    def downloadable(self) -> list[dict[str, Any]]:
        """What the dashboard offers for download."""
        out = []
        for name, path in (
            ("han_valid.jsonl", self.han_valid_path),
            ("han_invalid.jsonl", self.han_invalid_path),
            ("failed.jsonl", self.failed_path),
        ):
            exists = path.exists()
            out.append({
                "name": name,
                "available": exists,
                "lines": self._count(path),
                "bytes": path.stat().st_size if exists else 0,
            })
        return out

    def path_for(self, name: str) -> Path | None:
        """Resolve a download name to a path. Allowlist, not a path join —
        this is reachable from an HTTP route."""
        return {
            "han_valid.jsonl": self.han_valid_path,
            "han_invalid.jsonl": self.han_invalid_path,
            "failed.jsonl": self.failed_path,
        }.get(name)


class TeeResultSink:
    """Writes locally, and mirrors to a second sink when it is reachable.

    Local is authoritative. A multi-hour run must not lose its results because
    MinIO happened to be down at publish time, so a mirror failure is recorded
    and swallowed rather than failing the batch.
    """

    def __init__(self, primary: FileResultSink, mirror: "MinioResultSink") -> None:
        self.primary = primary
        self.mirror = mirror
        self.mirror_error = ""

    def _mirror(self, method: str, *args) -> None:
        try:
            getattr(self.mirror, method)(*args)
            self.mirror_error = ""
        except Exception as exc:  # noqa: BLE001 - local copy already succeeded
            self.mirror_error = f"{type(exc).__name__}: {exc}"
            log.warning("mirror sink failed (%s); local results are intact", exc)

    def write_results(self, records: list[HanScanRecord], scan_run_id: str) -> None:
        self.primary.write_results(records, scan_run_id)
        self._mirror("write_results", records, scan_run_id)

    def write_errors(self, errors: list[HanScanError], scan_run_id: str) -> None:
        self.primary.write_errors(errors, scan_run_id)
        self._mirror("write_errors", errors, scan_run_id)

    def write_run_summary(self, summary: dict[str, Any], scan_run_id: str) -> None:
        self.primary.write_run_summary(summary, scan_run_id)
        self._mirror("write_run_summary", summary, scan_run_id)


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
