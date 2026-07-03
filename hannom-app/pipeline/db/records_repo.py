"""Records repository — Postgres CRUD for extracted records.

The review editor reads/writes records here (source of truth), replacing the
per-job JSONL files. Every function returns/accepts the SAME dict shape the
frontend already uses (``id`` = the human record id like ``HVB_001.001.01``), so
the API responses are byte-for-byte compatible with the old JSONL path.

JSONB columns are decoded to Python objects by psycopg automatically on read;
on write we wrap them with ``Jsonb`` so dict/list values adapt correctly.
"""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb

from pipeline.db.conn import connect

# Columns stored as JSONB — wrapped with Jsonb on write.
_JSONB_COLS = {
    "han_conf", "entry_meta", "han_chars", "phonetic_per_char",
    "source_of", "han_bbox", "meaning_bbox",
}

# Record-dict keys that map to real columns (everything except the id alias).
_COLUMNS = [
    "human_id", "source_doc", "page", "line_no", "entry_no", "han", "han_raw",
    "han_conf", "phonetic", "meaning", "layout_type", "image_path", "entry_meta",
    "han_chars", "phonetic_per_char", "source_of", "review_status", "han_bbox",
    "meaning_bbox", "reviewed_by", "reviewed_at",
]


def _wrap(col: str, value):
    """Adapt a Python value for its column (JSONB columns need the Jsonb wrapper)."""
    if col in _JSONB_COLS:
        return Jsonb(value)
    return value


def _row_to_dict(row: dict) -> dict:
    """DB row → the record dict the frontend expects (``id`` = human_id)."""
    return {
        "id": row["human_id"],
        "source_doc": row["source_doc"],
        "page": row["page"],
        "line_no": row["line_no"],
        "han": row["han"],
        "han_raw": row["han_raw"],
        "han_conf": row["han_conf"],
        "phonetic": row["phonetic"],
        "meaning": row["meaning"],
        "layout_type": row["layout_type"],
        "image_path": row["image_path"],
        "entry_no": row["entry_no"],
        "entry_meta": row["entry_meta"],
        "han_chars": row["han_chars"],
        "phonetic_per_char": row["phonetic_per_char"],
        "source_of": row["source_of"],
        "review_status": row["review_status"],
        "han_bbox": row["han_bbox"],
        "meaning_bbox": row["meaning_bbox"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
    }


def _rec_to_row(job_id: int, rec: dict) -> dict:
    """A record dict (Record.to_dict() or an API body) → column→value map."""
    return {
        "human_id": rec["id"],
        "job_id": job_id,
        "source_doc": rec.get("source_doc", ""),
        "page": rec.get("page"),
        "line_no": rec.get("line_no"),
        "entry_no": rec.get("entry_no"),
        "han": rec.get("han", ""),
        "han_raw": rec.get("han_raw", ""),
        "han_conf": rec.get("han_conf", []),
        "phonetic": rec.get("phonetic", ""),
        "meaning": rec.get("meaning", ""),
        "layout_type": rec.get("layout_type", "two_column"),
        "image_path": rec.get("image_path", ""),
        "entry_meta": rec.get("entry_meta", {}),
        "han_chars": rec.get("han_chars", []),
        "phonetic_per_char": rec.get("phonetic_per_char", []),
        "source_of": rec.get("source_of", {}),
        "review_status": rec.get("review_status", "pending"),
        "han_bbox": rec.get("han_bbox"),
        "meaning_bbox": rec.get("meaning_bbox"),
        "reviewed_by": rec.get("reviewed_by"),
        "reviewed_at": rec.get("reviewed_at"),
    }


# ----------------------------------------------------------------------
def insert_many(dsn: str, job_id: int, records: list[dict]) -> int:
    """Bulk-insert extracted records for a job. Returns the count inserted."""
    if not records:
        return 0
    now = time.time()
    cols = _COLUMNS + ["job_id", "created_at"]
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO records ({', '.join(cols)}) VALUES ({placeholders})"
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            for rec in records:
                row = _rec_to_row(job_id, rec)
                vals = [_wrap(c, row[c]) for c in _COLUMNS] + [job_id, now]
                cur.execute(sql, vals)
        conn.commit()
    return len(records)


def list_by_job(dsn: str, job_id: int) -> list[dict]:
    """All records for a job, in reading order (page, then line)."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT * FROM records WHERE job_id=%s ORDER BY page NULLS FIRST, line_no, id",
            (job_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get(dsn: str, job_id: int, human_id: str) -> dict | None:
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT * FROM records WHERE job_id=%s AND human_id=%s",
            (job_id, human_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def update(dsn: str, job_id: int, human_id: str, changes: dict) -> dict | None:
    """Apply column changes to one record; returns the updated dict or None.

    ``changes`` keys are real column names (JSONB columns are wrapped here).
    """
    if not changes:
        return get(dsn, job_id, human_id)
    sets, vals = [], []
    for col, value in changes.items():
        sets.append(f"{col}=%s")
        vals.append(_wrap(col, value))
    vals.extend([job_id, human_id])
    with connect(dsn) as conn:
        row = conn.execute(
            f"UPDATE records SET {', '.join(sets)} WHERE job_id=%s AND human_id=%s RETURNING *",
            vals,
        ).fetchone()
        conn.commit()
    return _row_to_dict(row) if row else None


def create_one(dsn: str, job_id: int, rec: dict) -> dict:
    """Insert one record (a user-drawn box) and return it."""
    now = time.time()
    cols = _COLUMNS + ["job_id", "created_at"]
    row = _rec_to_row(job_id, rec)
    vals = [_wrap(c, row[c]) for c in _COLUMNS] + [job_id, now]
    placeholders = ", ".join(["%s"] * len(cols))
    with connect(dsn) as conn:
        out = conn.execute(
            f"INSERT INTO records ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
            vals,
        ).fetchone()
        conn.commit()
    return _row_to_dict(out)


def delete(dsn: str, job_id: int, human_id: str) -> bool:
    with connect(dsn) as conn:
        cur = conn.execute(
            "DELETE FROM records WHERE job_id=%s AND human_id=%s", (job_id, human_id)
        )
        conn.commit()
        return cur.rowcount > 0


def next_line_no(dsn: str, job_id: int, page: int) -> int:
    """Next line number for a page (for synthesizing a new record id)."""
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(line_no), 0) AS m FROM records WHERE job_id=%s AND page=%s",
            (job_id, page),
        ).fetchone()
    return int(row["m"]) + 1


def id_prefix(dsn: str, job_id: int) -> str:
    """The work-id prefix (e.g. 'HVB_001') from any existing record on the job."""
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT human_id FROM records WHERE job_id=%s ORDER BY id LIMIT 1", (job_id,)
        ).fetchone()
    if row and row["human_id"]:
        return row["human_id"].rsplit(".", 2)[0]
    return "HVB_001"
