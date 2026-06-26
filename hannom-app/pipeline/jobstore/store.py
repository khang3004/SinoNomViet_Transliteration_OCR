"""SQLite job store (AGENTS.md §3, §9).

The ``app`` creates jobs; the ``worker`` claims and runs them. SQLite (a single
file in the mounted ``data/`` volume) decouples the two services without a
broker — statelessness of the containers + state-in-volume is exactly what makes
a future K8s move trivial.

The public API (``create``, ``claim_next``, ``mark_done``, ``mark_failed``,
``get``, ``list_jobs``) is intentionally small and scheduler-friendly: an
external orchestrator (future Airflow DAG) can call ``create`` to enqueue work
without touching the worker.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: int
    filename: str
    input_path: str
    source_doc: str
    status: str
    output_path: str
    error: str
    created_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    input_path  TEXT NOT NULL,
    source_doc  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    output_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class JobStore:
    """Thin SQLite wrapper. Safe for the single-worker model in this task."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL lets the app read while the worker writes.
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    def create(self, filename: str, input_path: str, source_doc: str = "") -> int:
        """Enqueue a new job; returns its id."""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (filename, input_path, source_doc, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (filename, input_path, source_doc, JobStatus.PENDING.value, now, now),
            )
            return int(cur.lastrowid)

    def claim_next(self) -> Optional[Job]:
        """Atomically claim the oldest pending job, marking it RUNNING."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
                (JobStatus.PENDING.value,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT;")
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.RUNNING.value, now, row["id"]),
            )
            conn.execute("COMMIT;")
            return self._row_to_job(row, status=JobStatus.RUNNING.value, updated_at=now)

    def requeue_running(self) -> int:
        """Reset any ``running`` jobs back to ``pending`` and return the count.

        With the single-worker model, any job still ``running`` when the worker
        (re)starts was orphaned by a previous worker that died mid-job. Call this
        at startup so such jobs are retried instead of stuck forever.
        """
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE status = ?",
                (JobStatus.PENDING.value, now, JobStatus.RUNNING.value),
            )
            return cur.rowcount

    def mark_done(self, job_id: int, output_path: str) -> None:
        self._update(job_id, JobStatus.DONE.value, output_path=output_path, error="")

    def mark_failed(self, job_id: int, error: str) -> None:
        self._update(job_id, JobStatus.FAILED.value, error=error)

    def get(self, job_id: int) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    # ------------------------------------------------------------------
    def _update(self, job_id: int, status: str, **fields) -> None:
        now = time.time()
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, now]
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", vals)

    @staticmethod
    def _row_to_job(row: sqlite3.Row, **overrides) -> Job:
        data = dict(row)
        data.update(overrides)
        return Job(
            id=data["id"],
            filename=data["filename"],
            input_path=data["input_path"],
            source_doc=data["source_doc"],
            status=data["status"],
            output_path=data["output_path"],
            error=data["error"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
