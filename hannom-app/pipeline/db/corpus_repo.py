"""Corpus repository — read-only aggregation across ALL jobs.

Treats the whole corpus as one document keyed by Trang số (``records.page``),
regardless of which uploaded PDF (job) a page came from. Used by the Corpus view
to (a) list the sparse set of pages that have completed/verified work and (b) read
the entries on a given page. No schema of its own — pure queries over ``records``.
"""

from __future__ import annotations

from pipeline.db.conn import connect

# Only pages with at least one verified record count as "done" / in the corpus.
_VERIFIED = "count(*) FILTER (WHERE r.review_status='verified')"


def page_index(
    dsn: str, member_id: int | None = None, offset: int = 0, limit: int = 50
) -> tuple[list[dict], int]:
    """The sparse list of done pages (Trang số) + the total count for paging.

    When ``member_id`` is given, only that reviewer's records are counted (their
    done pages). Pages are aggregated across jobs.
    """
    where = ["r.page IS NOT NULL"]
    params: dict = {}
    if member_id is not None:
        where.append("r.reviewed_by = %(member)s")
        params["member"] = member_id
    where_sql = " AND ".join(where)
    with connect(dsn) as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM ("
            f"  SELECT r.page FROM records r WHERE {where_sql} "
            f"  GROUP BY r.page HAVING {_VERIFIED} > 0) t",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT r.page,
                   count(*) AS entries,
                   {_VERIFIED} AS verified,
                   array_agg(DISTINCT r.job_id ORDER BY r.job_id) AS jobs,
                   array_remove(array_agg(DISTINCT u.username), NULL) AS reviewers,
                   max(r.reviewed_at) AS last_active
            FROM records r
            LEFT JOIN users u ON u.id = r.reviewed_by
            WHERE {where_sql}
            GROUP BY r.page
            HAVING {_VERIFIED} > 0
            ORDER BY r.page
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
    return [dict(r) for r in rows], int(total)


def summary(dsn: str, member_id: int | None = None) -> dict:
    """Corpus totals for the header: done pages, total + verified entries."""
    where = ["r.page IS NOT NULL"]
    params: dict = {}
    if member_id is not None:
        where.append("r.reviewed_by = %(member)s")
        params["member"] = member_id
    where_sql = " AND ".join(where)
    with connect(dsn) as conn:
        page_count = conn.execute(
            f"SELECT count(*) AS n FROM ("
            f"  SELECT r.page FROM records r WHERE {where_sql} "
            f"  GROUP BY r.page HAVING {_VERIFIED} > 0) t",
            params,
        ).fetchone()["n"]
        agg = conn.execute(
            f"SELECT count(*) AS entries, {_VERIFIED} AS verified FROM records r WHERE {where_sql}",
            params,
        ).fetchone()
    return {
        "page_count": int(page_count),
        "entry_count": int(agg["entries"]),
        "verified_count": int(agg["verified"]),
    }


def page_entries(dsn: str, page: int) -> list[dict]:
    """Entries on one Trang số across all jobs, continuations folded into heads.

    Returns read-oriented dicts (entry_no, han, meaning, review_status, job_id,
    human_id, spans_pages) ordered by (job_id, line_no).
    """
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT human_id, job_id, page, line_no, entry_no, han, meaning, "
            "       review_status, part_of "
            "FROM records WHERE page=%s ORDER BY job_id, line_no, id",
            (page,),
        ).fetchall()
    recs = [dict(r) for r in rows]
    by_id = {r["human_id"]: r for r in recs}
    children: dict[str, list[dict]] = {}
    heads: list[dict] = []
    for r in recs:
        head = r.get("part_of")
        # Only fold into a head that is on THIS page; otherwise treat as its own row.
        if head and head in by_id:
            children.setdefault(head, []).append(r)
        else:
            heads.append(r)
    out: list[dict] = []
    for h in heads:
        parts = [h] + sorted(
            children.get(h["human_id"], []),
            key=lambda r: (r.get("job_id") or 0, r.get("line_no") or 0),
        )
        han = "".join(p.get("han", "") for p in parts)
        meaning = " ".join(
            p.get("meaning", "").strip() for p in parts if p.get("meaning", "").strip()
        )
        out.append({
            "human_id": h["human_id"],
            "job_id": h["job_id"],
            "entry_no": h.get("entry_no"),
            "han": han,
            "meaning": meaning,
            "review_status": h.get("review_status"),
            "spans_pages": len(parts) > 1,
        })
    return out
