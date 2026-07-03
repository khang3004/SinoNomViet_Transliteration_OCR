"""Postgres-backed job queue — a drop-in for the SQLite ``JobStore``.

Implements the exact same public API (``create / claim_next / requeue_running /
mark_done / mark_failed / get / list_jobs``) so app + worker code is unchanged
except for how the store is constructed (see ``pipeline.jobstore.get_store``).

The win over SQLite: ``claim_next`` uses ``FOR UPDATE SKIP LOCKED``, so multiple
workers can pull jobs concurrently without blocking each other — and app reads
never block on a worker write (the ``/jobs`` hang under Docker is gone).
"""

from __future__ import annotations

import time
from typing import Optional

from pipeline.db.conn import connect, init_db
from pipeline.jobstore.store import Job, JobStatus


class PostgresJobStore:
    """Job queue on Postgres. Safe for MULTIPLE concurrent workers."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        init_db(dsn)  # ensure schema (idempotent), mirrors SQLite JobStore

    # ------------------------------------------------------------------
    def create(
        self,
        filename: str,
        input_path: str,
        source_doc: str = "",
        kind: str = "extract",
        payload: str = "",
    ) -> int:
        now = time.time()
        with connect(self._dsn) as conn:
            row = conn.execute(
                "INSERT INTO jobs (filename, input_path, source_doc, status, "
                "created_at, updated_at, kind, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (filename, input_path, source_doc, JobStatus.PENDING.value, now, now, kind, payload),
            ).fetchone()
            return int(row["id"])

    def claim_next(self) -> Optional[Job]:
        """Atomically claim the oldest pending job across all workers."""
        now = time.time()
        with connect(self._dsn) as conn:
            row = conn.execute(
                "UPDATE jobs SET status=%s, updated_at=%s "
                "WHERE id = (SELECT id FROM jobs WHERE status=%s "
                "           ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING *",
                (JobStatus.RUNNING.value, now, JobStatus.PENDING.value),
            ).fetchone()
            return _row_to_job(row) if row else None

    def requeue_running(self) -> int:
        """Reset orphaned ``running`` jobs to ``pending`` (worker-restart recovery).

        NOTE: with several workers this also requeues siblings' in-flight jobs if
        they happen to start together; acceptable at this scale (controlled
        startup, idempotent re-run). A lease/heartbeat would remove the race.
        """
        now = time.time()
        with connect(self._dsn) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=%s, updated_at=%s WHERE status=%s",
                (JobStatus.PENDING.value, now, JobStatus.RUNNING.value),
            )
            return cur.rowcount

    def mark_done(self, job_id: int, output_path: str) -> None:
        self._update(job_id, JobStatus.DONE.value, output_path=output_path, error="")

    def mark_failed(self, job_id: int, error: str) -> None:
        self._update(job_id, JobStatus.FAILED.value, error=error)

    def set_result(self, job_id: int, result: str) -> None:
        """Store a small inline result (e.g. re-OCR text) on the job row."""
        self._update(job_id, JobStatus.DONE.value, result=result, error="")

    def get(self, job_id: int) -> Optional[Job]:
        with connect(self._dsn) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return _row_to_job(row) if row else None

    def get_result(self, job_id: int) -> str:
        """Return the inline ``result`` text for a job ('' if none)."""
        with connect(self._dsn) as conn:
            row = conn.execute("SELECT result FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return (row or {}).get("result", "") or ""

    def list_jobs(self, limit: int = 100) -> list[Job]:
        with connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT %s", (limit,)
            ).fetchall()
            return [_row_to_job(r) for r in rows]

    # ------------------------------------------------------------------
    def _update(self, job_id: int, status: str, **fields) -> None:
        now = time.time()
        sets = ["status=%s", "updated_at=%s"]
        vals: list = [status, now]
        for k, v in fields.items():
            sets.append(f"{k}=%s")
            vals.append(v)
        vals.append(job_id)
        with connect(self._dsn) as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=%s", vals)


def _row_to_job(row: dict) -> Job:
    """Map a jobs row (dict) → Job. Extra columns (result/created_by) ignored."""
    return Job(
        id=row["id"],
        filename=row["filename"],
        input_path=row["input_path"],
        source_doc=row["source_doc"],
        status=row["status"],
        output_path=row["output_path"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        kind=row.get("kind", "extract"),
        payload=row.get("payload", ""),
    )
