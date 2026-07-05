"""Assignments repository — per-user page-range ownership on a job.

A reviewer may VIEW the whole document but only EDIT records whose page falls in
one of their assigned inclusive [page_start, page_end] ranges for that job.
"""

from __future__ import annotations

import time

from pipeline.db.conn import connect


def create(dsn: str, user_id: int, job_id: int, page_start: int, page_end: int) -> dict:
    lo, hi = sorted((int(page_start), int(page_end)))
    with connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO assignments (user_id, job_id, page_start, page_end, created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id, user_id, job_id, page_start, page_end",
            (user_id, job_id, lo, hi, time.time()),
        ).fetchone()
        conn.commit()
    return dict(row)


def list_all(dsn: str) -> list[dict]:
    """All assignments joined with the reviewer's username (admin view)."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT a.id, a.user_id, u.username, a.job_id, a.page_start, a.page_end "
            "FROM assignments a JOIN users u ON u.id = a.user_id ORDER BY a.job_id, a.page_start"
        ).fetchall()
    return [dict(r) for r in rows]


def list_for_user(dsn: str, user_id: int) -> list[dict]:
    """All of one reviewer's assignments (across jobs)."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, job_id, page_start, page_end FROM assignments "
            "WHERE user_id=%s ORDER BY job_id, page_start",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def ranges_for(dsn: str, user_id: int, job_id: int) -> list[tuple[int, int]]:
    """The (start, end) ranges a reviewer owns on a specific job."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT page_start, page_end FROM assignments WHERE user_id=%s AND job_id=%s",
            (user_id, job_id),
        ).fetchall()
    return [(r["page_start"], r["page_end"]) for r in rows]


def covers(dsn: str, user_id: int, job_id: int, page: int | None) -> bool:
    """True if the reviewer has a range covering ``page`` on this job."""
    if page is None:
        return False
    return any(lo <= page <= hi for (lo, hi) in ranges_for(dsn, user_id, job_id))


def delete(dsn: str, assignment_id: int) -> bool:
    with connect(dsn) as conn:
        cur = conn.execute("DELETE FROM assignments WHERE id=%s", (assignment_id,))
        conn.commit()
        return cur.rowcount > 0


def progress(dsn: str) -> list[dict]:
    """Per-assignment review progress for the admin dashboard.

    For each reviewer's page range on a job: how many records are verified vs
    total, the first page still needing work (``current_page``), and the last time
    the reviewer verified anything (``last_active``, epoch seconds).
    """
    with connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.user_id, u.username, a.job_id, a.page_start, a.page_end,
              (SELECT count(*) FROM records r
                 WHERE r.job_id=a.job_id AND r.page BETWEEN a.page_start AND a.page_end) AS total,
              (SELECT count(*) FROM records r
                 WHERE r.job_id=a.job_id AND r.page BETWEEN a.page_start AND a.page_end
                   AND r.review_status='verified') AS verified,
              (SELECT min(r.page) FROM records r
                 WHERE r.job_id=a.job_id AND r.page BETWEEN a.page_start AND a.page_end
                   AND r.review_status <> 'verified') AS current_page,
              (SELECT max(r.reviewed_at) FROM records r
                 WHERE r.job_id=a.job_id AND r.reviewed_by=a.user_id) AS last_active
            FROM assignments a JOIN users u ON u.id = a.user_id
            ORDER BY u.username, a.job_id, a.page_start
            """
        ).fetchall()
    return [dict(r) for r in rows]
