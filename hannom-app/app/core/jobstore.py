"""Crash-survivable job state on disk. No database.

Layout, one directory per batch::

    data/jobs/<job_id>/
      job.json         phase, counts, heartbeat, error
      manifest.jsonl   work list - written once, never mutated
      downloads.jsonl  append-only, one line per download outcome
      results.jsonl    append-only, one line per OCR outcome
      events.jsonl     append-only, the UI's live log

Everything terminal is append-only. At 20k items, rewriting a manifest on every
completion would be O(n^2); appending is O(1) and a crash costs only in-flight
work. Resume replays the logs and skips indices already terminal.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

# A job whose heartbeat is older than this is presumed dead (container killed).
HEARTBEAT_STALE_S = 120.0


class Phase(str, Enum):
    PENDING = "pending"
    PREFLIGHT = "preflight"
    DOWNLOAD = "download"
    OCR = "ocr"
    PUBLISH = "publish"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {Phase.DONE, Phase.FAILED, Phase.CANCELLED, Phase.INTERRUPTED}


def new_run_id() -> str:
    """Timestamped like the crawler's run ids, so the two logs sort together."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def new_job_id() -> str:
    return f"{new_run_id()}_{uuid.uuid4().hex[:6]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobCounts:
    total_posts: int = 0
    total_images: int = 0
    downloaded: int = 0
    download_failed: int = 0
    scanned: int = 0
    scan_failed: int = 0
    han_valid: int = 0
    han_invalid: int = 0
    ready_for_ocr: int = 0
    published: int = 0


@dataclass
class JobState:
    job_id: str
    scan_run_id: str
    phase: Phase = Phase.PENDING
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)
    heartbeat_at: str = field(default_factory=_utcnow)
    heartbeat_monotonic: float = 0.0
    counts: JobCounts = field(default_factory=JobCounts)
    preflight: dict[str, Any] = field(default_factory=dict)
    source_keys: list[str] = field(default_factory=list)
    source_run_ids: list[str] = field(default_factory=list)
    corpus_total: int = 0
    processed_total: int = 0
    runs_total: int = 0
    runs_done: int = 0
    ocr_workers: int = 0
    error: str = ""
    cancel_requested: bool = False
    # Set when preflight finds expired URLs and the job waits for a human.
    awaiting_confirmation: bool = False
    limit: int = 0
    # False = download and sign only; OCR happens downstream.
    run_ocr: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scan_run_id": self.scan_run_id,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "counts": self.counts.__dict__,
            "preflight": self.preflight,
            "source_keys": self.source_keys,
            "source_run_ids": self.source_run_ids,
            "corpus_total": self.corpus_total,
            "processed_total": self.processed_total,
            "runs_total": self.runs_total,
            "runs_done": self.runs_done,
            "ocr_workers": self.ocr_workers,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "awaiting_confirmation": self.awaiting_confirmation,
            "limit": self.limit,
            "run_ocr": self.run_ocr,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "JobState":
        state = cls(
            job_id=data["job_id"],
            scan_run_id=data.get("scan_run_id", ""),
            phase=Phase(data.get("phase", "pending")),
            created_at=data.get("created_at", _utcnow()),
            updated_at=data.get("updated_at", _utcnow()),
            heartbeat_at=data.get("heartbeat_at", _utcnow()),
            preflight=data.get("preflight", {}),
            source_keys=data.get("source_keys", []),
            source_run_ids=data.get("source_run_ids", []),
            corpus_total=data.get("corpus_total", 0),
            processed_total=data.get("processed_total", 0),
            runs_total=data.get("runs_total", 0),
            runs_done=data.get("runs_done", 0),
            ocr_workers=data.get("ocr_workers", 0),
            error=data.get("error", ""),
            cancel_requested=data.get("cancel_requested", False),
            awaiting_confirmation=data.get("awaiting_confirmation", False),
            limit=data.get("limit", 0),
            run_ocr=data.get("run_ocr", True),
        )
        for key, value in (data.get("counts") or {}).items():
            if hasattr(state.counts, key):
                setattr(state.counts, key, value)
        return state


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON line and fsync it.

    fsync costs a few ms but is the difference between "resumable" and
    "resumable unless the container died in the last second".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A torn last line from a hard kill; everything before it stands.
                log.warning("skipping malformed line in %s", path)


class JobDir:
    """One batch's on-disk state."""

    def __init__(self, root: Path, job_id: str) -> None:
        self.root = root / job_id
        self.job_id = job_id

    @property
    def job_file(self) -> Path: return self.root / "job.json"
    @property
    def manifest_file(self) -> Path: return self.root / "manifest.jsonl"
    @property
    def downloads_file(self) -> Path: return self.root / "downloads.jsonl"
    @property
    def results_file(self) -> Path: return self.root / "results.jsonl"
    @property
    def events_file(self) -> Path: return self.root / "events.jsonl"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # --- job.json --------------------------------------------------------

    def save_state(self, state: JobState) -> None:
        self.ensure()
        state.updated_at = _utcnow()
        tmp = self.job_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.job_file)  # atomic: never a half-written job.json

    def load_state(self) -> JobState | None:
        if not self.job_file.exists():
            return None
        try:
            return JobState.from_json(json.loads(self.job_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("unreadable job.json for %s: %s", self.job_id, exc)
            return None

    def heartbeat(self, state: JobState) -> None:
        state.heartbeat_at = _utcnow()
        state.heartbeat_monotonic = time.monotonic()
        self.save_state(state)

    def is_stale(self) -> bool:
        """Heartbeat older than the threshold => the process died mid-run."""
        if not self.job_file.exists():
            return False
        age = time.time() - self.job_file.stat().st_mtime
        return age > HEARTBEAT_STALE_S

    # --- manifest (immutable) -------------------------------------------

    def write_manifest(self, items: list[dict[str, Any]]) -> None:
        self.ensure()
        with open(self.manifest_file, "w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_manifest(self) -> list[dict[str, Any]]:
        return list(_read_lines(self.manifest_file))

    # --- append-only progress -------------------------------------------

    def append_download(self, payload: dict[str, Any]) -> None:
        _append_line(self.downloads_file, payload)

    def append_result(self, payload: dict[str, Any]) -> None:
        _append_line(self.results_file, payload)

    def append_event(self, level: str, message: str, **extra: Any) -> None:
        _append_line(
            self.events_file,
            {"ts": _utcnow(), "level": level, "message": message, **extra},
        )

    def read_downloads(self) -> list[dict[str, Any]]:
        return list(_read_lines(self.downloads_file))

    def read_results(self) -> list[dict[str, Any]]:
        return list(_read_lines(self.results_file))

    def read_events(self, cursor: int = 0, limit: int = 200) -> tuple[list[dict], int]:
        """Events after ``cursor``, plus the new cursor.

        Cursor-based rather than streamed so a browser that reconnects, or one
        opened hours later, gets exactly the lines it has not seen.
        """
        events = list(_read_lines(self.events_file))
        window = events[cursor : cursor + limit]
        return window, min(cursor + len(window), len(events))

    def event_count(self) -> int:
        return sum(1 for _ in _read_lines(self.events_file))

    # --- resume ----------------------------------------------------------

    def completed_download_keys(self) -> dict[str, dict[str, Any]]:
        """Terminal download outcomes keyed ``post_id:idx``."""
        return {f"{d.get('post_id')}:{d.get('idx')}": d for d in self.read_downloads()}

    def completed_scan_keys(self) -> dict[str, dict[str, Any]]:
        return {f"{r.get('post_id')}:{r.get('idx')}": r for r in self.read_results()}


class JobStore:
    """Discovery and lifecycle across all job directories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, scan_run_id: str, limit: int) -> tuple[JobDir, JobState]:
        job_id = new_job_id()
        job_dir = JobDir(self.root, job_id)
        job_dir.ensure()
        state = JobState(job_id=job_id, scan_run_id=scan_run_id, limit=limit)
        job_dir.save_state(state)
        job_dir.append_event("info", f"job {job_id} created (limit={limit})")
        return job_dir, state

    def get(self, job_id: str) -> JobDir | None:
        job_dir = JobDir(self.root, job_id)
        return job_dir if job_dir.job_file.exists() else None

    def list_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        ids = [p.name for p in self.root.iterdir() if p.is_dir() and (p / "job.json").exists()]
        return sorted(ids, reverse=True)  # newest first

    def list_states(self, limit: int = 50) -> list[JobState]:
        out: list[JobState] = []
        for job_id in self.list_ids()[:limit]:
            state = JobDir(self.root, job_id).load_state()
            if state is not None:
                out.append(state)
        return out

    def active(self) -> JobState | None:
        """The one job currently running, if any."""
        for state in self.list_states():
            if not state.phase.terminal:
                return state
        return None

    def reap_interrupted(self) -> list[str]:
        """Mark jobs whose process died as INTERRUPTED so the UI can offer Resume.

        Called at startup. Without this, a container restart leaves a job stuck
        showing 'ocr' forever with nothing driving it.
        """
        reaped: list[str] = []
        for job_id in self.list_ids():
            job_dir = JobDir(self.root, job_id)
            state = job_dir.load_state()
            if state is None or state.phase.terminal:
                continue
            if job_dir.is_stale():
                state.phase = Phase.INTERRUPTED
                state.error = "process died mid-run (stale heartbeat)"
                job_dir.save_state(state)
                job_dir.append_event("warn", "job marked interrupted after restart")
                reaped.append(job_id)
        return reaped
