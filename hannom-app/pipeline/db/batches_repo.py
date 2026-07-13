"""AI batch auto-scan tracking (Postgres). Stores the Gemini batch NAME only.

The API key is never persisted — the browser re-supplies it on each poll. This
table lets any admin see an in-flight batch and resume polling after a reload.
"""

from __future__ import annotations

import time

from pipeline.db.conn import connect


def create(dsn: str, job_id: int, batch_name: str, provider: str, model: str,
           pages: int, created_by: int | None) -> dict:
    with connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO autoscan_batches "
            "(job_id, batch_name, provider, model, state, pages, created_by, created_at) "
            "VALUES (%s,%s,%s,%s,'submitted',%s,%s,%s) RETURNING *",
            (job_id, batch_name, provider, model, pages, created_by, time.time()),
        ).fetchone()
        conn.commit()
    return dict(row)


def list_for_job(dsn: str, job_id: int) -> list[dict]:
    """A job's batches, newest first (for the resume UI)."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT * FROM autoscan_batches WHERE job_id=%s ORDER BY created_at DESC",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_name(dsn: str, job_id: int, batch_name: str) -> dict | None:
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT * FROM autoscan_batches WHERE job_id=%s AND batch_name=%s",
            (job_id, batch_name),
        ).fetchone()
    return dict(row) if row else None


def set_state(dsn: str, batch_name: str, state: str, error: str = "") -> None:
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE autoscan_batches SET state=%s, error=%s WHERE batch_name=%s",
            (state, error, batch_name),
        )
        conn.commit()


def mark_applied(dsn: str, batch_name: str, created_entries: int) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE autoscan_batches SET state='applied', created_entries=%s, applied_at=%s "
            "WHERE batch_name=%s",
            (created_entries, time.time(), batch_name),
        )
        conn.commit()
