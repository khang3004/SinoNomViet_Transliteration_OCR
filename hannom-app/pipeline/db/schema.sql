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

-- Extracted records — the source of truth for the review editor (replaces the
-- per-job JSONL files). Mirrors the Record dataclass (pipeline/schema.py); the
-- app-facing record id ("HVB_001.001.01") is stored as human_id, unique per job.
-- List/array fields (bboxes, chars, meta, provenance) are JSONB. Timestamps are
-- epoch seconds (DOUBLE PRECISION) for parity with the rest of the app.
CREATE TABLE IF NOT EXISTS records (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    human_id          TEXT NOT NULL,
    job_id            BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_doc        TEXT NOT NULL DEFAULT '',
    page              INTEGER,
    line_no           INTEGER,
    entry_no          INTEGER,
    han               TEXT NOT NULL DEFAULT '',
    han_raw           TEXT NOT NULL DEFAULT '',
    han_conf          JSONB NOT NULL DEFAULT '[]',
    phonetic          TEXT NOT NULL DEFAULT '',
    meaning           TEXT NOT NULL DEFAULT '',
    layout_type       TEXT NOT NULL DEFAULT 'two_column',
    image_path        TEXT NOT NULL DEFAULT '',
    entry_meta        JSONB NOT NULL DEFAULT '{}',
    han_chars         JSONB NOT NULL DEFAULT '[]',
    phonetic_per_char JSONB NOT NULL DEFAULT '[]',
    source_of         JSONB NOT NULL DEFAULT '{}',
    review_status     TEXT NOT NULL DEFAULT 'pending',
    han_bbox          JSONB,
    meaning_bbox      JSONB,
    reviewed_by       INTEGER,
    reviewed_at       DOUBLE PRECISION,
    created_at        DOUBLE PRECISION NOT NULL,
    UNIQUE (job_id, human_id)
);

CREATE INDEX IF NOT EXISTS idx_records_job ON records (job_id);
CREATE INDEX IF NOT EXISTS idx_records_job_page ON records (job_id, page);
