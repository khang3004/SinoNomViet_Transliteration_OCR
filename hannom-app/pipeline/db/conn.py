"""Postgres connection + schema init (psycopg 3).

One tiny helper module so every DB user opens connections the same way. Kept
dependency-light: ``psycopg`` is imported here, so importing this module at all
requires the driver — callers gate on ``config.database_url`` before importing.
"""

from __future__ import annotations

import logging
import os
import re
import threading

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("hannom.db")

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
_init_lock = threading.Lock()
_initialised: set[str] = set()


def connect(dsn: str) -> psycopg.Connection:
    """Open a new connection. Use as a context manager (commits/rolls back on exit).

    Rows come back as dicts so callers can map straight onto dataclasses.
    """
    return psycopg.connect(dsn, row_factory=dict_row)


def init_db(dsn: str) -> None:
    """Apply schema.sql idempotently. Runs once per DSN per process.

    schema.sql is plain ``CREATE TABLE/INDEX IF NOT EXISTS`` (no function bodies).
    We strip ``--`` line comments first so a ``;`` inside a comment can't split a
    statement, then split on ``;``. Guarded by a lock + a seen-set so concurrent
    startup (app + worker) doesn't race or repeat the work.
    """
    with _init_lock:
        if dsn in _initialised:
            return
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            script = fh.read()
        script = re.sub(r"--[^\n]*", "", script)  # drop line comments
        statements = [s.strip() for s in script.split(";") if s.strip()]
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
            conn.commit()
        _initialised.add(dsn)
        logger.info("Postgres schema ensured (%d statement(s)).", len(statements))
