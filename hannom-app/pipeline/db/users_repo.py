"""Users repository — Postgres CRUD for login accounts.

Only stores bcrypt password HASHES (see app/auth.py for hashing). ``get_by_*``
returns the hash for verification; ``list_users`` never does.
"""

from __future__ import annotations

import time

from pipeline.db.conn import connect


def count(dsn: str) -> int:
    with connect(dsn) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def create(dsn: str, username: str, password_hash: str, role: str = "reviewer") -> dict:
    with connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id, username, role, created_at",
            (username, password_hash, role, time.time()),
        ).fetchone()
        conn.commit()
    return dict(row)


def get_by_username(dsn: str, username: str) -> dict | None:
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username=%s",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_by_id(dsn: str, user_id: int) -> dict | None:
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id=%s", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def set_password(dsn: str, user_id: int, password_hash: str) -> bool:
    """Update a user's bcrypt password hash (admin password reset)."""
    with connect(dsn) as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def list_users(dsn: str) -> list[dict]:
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
