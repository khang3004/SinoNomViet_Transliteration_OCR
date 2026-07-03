-- hannom-app Postgres schema (idempotent). Applied at app/worker startup by
-- pipeline/db/conn.py:init_db. Split and executed statement-by-statement.
--
-- Single document workflow ("Châu Bản"). Jobs = the async queue shared by app
-- (enqueue) and worker (claim/run). created_at/updated_at are epoch seconds
-- (DOUBLE PRECISION) to match the existing Job dataclass 1:1. payload/result
-- stay TEXT (opaque JSON strings the app already json.dumps/loads).

CREATE TABLE IF NOT EXISTS jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename    TEXT NOT NULL,
    input_path  TEXT NOT NULL DEFAULT '',
    source_doc  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    output_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'extract',
    payload     TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '',
    created_by  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs (status, id);
