# hannom-app — Complete Documentation

A self-hosted, multi-user tool for building a **parallel Hán ↔ Quốc-ngữ corpus**
from the *Mục lục Châu bản triều Nguyễn* (Nguyễn-dynasty royal-records catalogue).
It OCRs uploaded PDFs, lets reviewers correct and verify the results page-by-page,
and exports clean JSONL for training machine-translation models.

> Companion docs: `docs/DEPLOYMENT.md` (hosting runbook),
> `docs/SUPABASE_MIGRATION.md` (superseded design note — kept for reference).

---

## Table of contents
1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Data model](#3-data-model)
4. [The extraction pipeline](#4-the-extraction-pipeline)
5. [Features](#5-features)
6. [Roles & permissions](#6-roles--permissions)
7. [End-to-end workflow](#7-end-to-end-workflow)
8. [API reference](#8-api-reference)
9. [Configuration (env vars)](#9-configuration-env-vars)
10. [Deployment](#10-deployment)
11. [Operations & troubleshooting](#11-operations--troubleshooting)
12. [Development](#12-development)
13. [Known limitations & notes](#13-known-limitations--notes)

---

## 1. What it does

- **Upload** a text-layer Châu bản PDF (or image). The worker rasterises each page,
  reads the **Vietnamese** from the PDF text layer and the **Hán** from OCR of the
  left column, and pairs them per entry (bài).
- **Review**: reviewers open a job, see each entry's Hán/Việt boxes over the page
  image, fix the text, adjust boxes, re-OCR a region, ask an LLM to correct or read
  it, and **verify**.
- **Assign**: an admin gives each reviewer a **page range**; reviewers can only edit
  their pages.
- **Browse**: a **Corpus** view aggregates finished pages across all uploads.
- **Export**: `.jsonl`, one line per entry (Hán · Quốc-ngữ · metadata), regenerated
  live from the database so edits are always reflected.

Primary document type = `two_column` (Mục lục). The layout + OCR engines are
**pluggable registries**, so other document shapes/engines can be added.

---

## 2. Architecture

Three containers, defined in `docker-compose.yml`:

```
   Browsers ──HTTPS──▶ Caddy (TLS) ──▶ app  (FastAPI, :8000, stateless, no GPU)
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                     ▼
              Postgres (:5432)   ◀─ job queue ─▶   worker(s)         ./data (bind mount)
              users, jobs,          claim/run       PaddleOCR         uploads/, output/pages/
              records, assignments                  CPU or GPU        output/*.jsonl
              [volume: pgdata]                                        [source PDFs + page PNGs]
```

- **app** (`app/`, `Dockerfile.app`, `python:3.12-slim`) — serves the single-page UI
  and the REST API, runs auth, and makes the synchronous LLM calls with each user's
  own key. Holds **no secrets of its own**, no GPU. ~350 MB image.
- **worker** (`worker/`, `Dockerfile.worker`) — polls the Postgres queue and runs the
  OCR/extraction pipeline (PaddleOCR + poppler). CPU by default, GPU-capable.
  ~1.8 GB image. **Can be scaled** (`--scale worker=N`).
- **postgres** (`postgres:16-alpine`) — the source of truth. Tiny footprint.
- **Shared state**: `pgdata` named volume (DB) + `./data` bind mount (uploaded files,
  rendered page images, JSONL exports).

App and worker are **decoupled by the Postgres job queue**, so a worker crash never
loses a reviewer's edits and OCR runs in the background.

**Pipeline code** (`pipeline/`) is bind-mounted into both containers, so most code
changes deploy with a `git pull` + `docker compose restart` — no image rebuild.

---

## 3. Data model

Postgres schema in `pipeline/db/schema.sql`, applied idempotently at startup by
`pipeline/db/conn.py:init_db` (with `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
migrations for additive changes).

| Table | Purpose | Key columns |
|---|---|---|
| `users` | login accounts | `id, username (unique), password_hash (bcrypt), role (admin\|reviewer)` |
| `jobs` | the async work queue | `id, filename, input_path, status (pending\|running\|done\|failed), kind (extract\|reocr), output_path, error, payload, result` |
| `records` | extracted entries (source of truth for the editor) | `human_id (e.g. HVB_001.001.01), job_id→jobs, page (Trang số), line_no, entry_no (Bài số), han, han_raw, meaning, entry_meta (jsonb), han_bbox/meaning_bbox (jsonb), review_status, reviewed_by→users, reviewed_at, part_of` |
| `assignments` | per-reviewer page ranges | `id, user_id→users, job_id→jobs, page_start, page_end` |

- `records.job_id` and `assignments.job_id` are **`ON DELETE CASCADE`** — deleting a
  job removes its records + assignments automatically.
- `records.part_of` links a **spanning entry**'s continuation to its head (see §5).
- Timestamps are epoch seconds (`double precision`) to match the code's `Job`/`Record`
  shapes.

The queue and repositories live in `pipeline/db/` (`postgres_store.py`,
`records_repo.py`, `users_repo.py`, `assignments_repo.py`, `corpus_repo.py`).
`pipeline/jobstore/get_store(config)` returns the Postgres store when `DATABASE_URL`
is set, else a SQLite store (dev/tests, no `psycopg` needed).

---

## 4. The extraction pipeline

`pipeline/runner.py:process_file` routes an upload through two **registries**:

- **OCR engines** (`pipeline/ocr/`): `paddle` (default, PaddleOCR `chinese_cht`),
  `vision` (Google Vision). Selected by `OCR_BACKEND`.
- **Layout handlers** (`pipeline/layouts/`): `two_column` (primary — Mục lục),
  `three_block`, `han_only`. The router picks the first whose `detect()` matches.

**`two_column`** (`pipeline/layouts/two_column.py`) — the core:
1. Detect a Mục lục page by its label fingerprint (`TRÍCH YẾU`, `Loại:`, `Xuất xứ`, …).
2. Derive the Hán|Việt column split from the text-layer x-distribution.
3. Group right-column spans into **entries** anchored on date lines
   (`6 tháng 7 năm …` **and** `Ngày: …`-prefixed dates).
4. OCR only the **left-column crop** (Hán), starting **below "TRÍCH YẾU"**.
5. Pair Hán ↔ Việt per entry by y-overlap; parse metadata (Ngày/Tờ-Tập/Loại/Xuất
   xứ/Đề tài); extract **Bài số** (the standalone integer left of `Loại:`, ignoring
   the Tờ/Tập folio number).
6. **Watermark removal** (`pipeline/imageproc.py`): Otsu binarisation drops the light
   "LƯU TRỮ VN" watermark before OCR (best-effort; never breaks OCR).

**Per-page resilience**: each page is isolated in a try/except, so one unrenderable
scan or non-Mục-lục divider page is **logged and skipped** instead of failing the
whole (300-page) job.

**Output**: `Record` objects (`pipeline/schema.py`) → inserted into Postgres by the
worker (and written as a JSONL artifact). Each record carries `han`, `meaning`,
`entry_meta`, bounding boxes, provenance (`source_of`), and page/entry numbers.

---

## 5. Features

- **Login** — self-hosted: bcrypt passwords, JWT in an httpOnly cookie (`app/auth.py`).
  A gate middleware protects every non-public route. `COOKIE_SECURE=1` marks the
  cookie Secure behind HTTPS.
- **Page-range assignments** — admin assigns each reviewer `[start,end]` pages on a
  job. **View-all / edit-own**: reviewers can view pages for context but only
  edit/verify their assigned pages (others are read-only). Reviewers also **only see
  the jobs they're assigned to**.
- **Review editor** (`app/static/index.html`) — clickable/editable Hán + Việt boxes
  over the page image (drag/resize/draw/delete), **re-OCR a box**, edit-in-place, a
  verify checkbox that gates Save.
- **LLM assist (bring-your-own-key)** — each reviewer pastes their **own** provider
  key in the UI; it is sent per-request and **never stored or logged**:
  - **AI-correct** — proofread the Hán using the Vietnamese as context.
  - **Translate** — Hán → modern Vietnamese.
  - **👁 Vision** — draw a box → the browser crops it → the model **reads the Hán
    straight from the image** + the current OCR text (great for missed/garbled
    regions). Providers: **gemini, openai, anthropic** (vision) and **deepseek**
    (text only — the 👁 button is disabled for it). Anthropic vision uses
    `claude-3-5-sonnet`.
- **🤖 Batch AI scan (bring-your-own-key)** — in a job's review toolbar, queue a
  **Gemini Batch** job over the job's **unverified pages** (optional from→to range).
  Gemini reads each whole page asynchronously (minutes–hours, ~50% cheaper, no
  rate-limit failures): it places the Hán + Việt boxes, OCRs both, reads the entry
  number, detects **continuations** (page breaks), and fills best-effort metadata.
  When the batch finishes the app **auto-applies** the results as **pending** records
  (verify tick stays off) for a human to check. On a page with no verified entries it
  **replaces** the non-verified records; pages with any verified entry are **skipped**.
  Continuation links are derived from reading order (never an LLM id), so re-running is
  safe. Only the batch **name** is persisted (never the key), so an in-flight batch is
  shown and resumes polling when the job is reopened. Default model
  `gemini-2.5-flash`. (The interactive per-box **LLM OCR** stays synchronous.)
- **Spanning entries (page breaks)** — a bài whose text continues on the next page:
  select the continuation → **"⤷ mark as continuation of previous entry"**. Parts
  keep their own boxes but **merge into one entry** for export and counting
  (`records.part_of`; export concatenates `han`/`meaning` and records `spans_pages`).
  Linking **auto-fills** the fragment from its head (`entry_no` + Ngày/Tờ-Tập/Loại/
  Xuất xứ/Đề tài) and it **shares the head's verified status** (verifying a head
  cascades to its continuations), so a spanning bài never reads as half-pending.
- **Admin progress dashboard** — per reviewer × range: verified/total, the page
  they're currently on, and last-active time (auto-refreshes).
- **📚 Corpus view** — treats all uploads as one corpus keyed by **Trang số**. Lists
  only pages with verified work (sparse), paginated; click a page to **read** its
  merged entries. Everyone can browse; admins get a per-member filter.
- **☁ Google Sheets export ("Sync sheet")** — admin-only button in the Corpus tab
  that pushes the **verified** corpus into a shared Google Sheet with two tabs:
  **Hán–Việt** (two columns) and **Chi tiết** (Trang số, Entry, Hán, Việt, and the
  catalogue metadata + Upload/Người duyệt). Full-replace (idempotent). Auth is a
  **service account**; the sheet is pre-shared with it. See §9 for setup.
- **Job management** — admin-only **upload** and **🗑 delete job** (removes records +
  assignments via cascade and the job's files). **Admin can reset any user's
  password** from the Users table.

---

## 6. Roles & permissions

| Action | Admin | Reviewer |
|---|---|---|
| Upload a PDF | ✅ | ❌ (403; Upload card hidden) |
| See jobs list | all jobs | **only assigned jobs** |
| Open / read a job's records | any | only assigned jobs (else 403) |
| Edit / verify a record | any page | only pages in an assigned range |
| Delete a job | ✅ | ❌ |
| Create reviewers, assign ranges | ✅ | ❌ |
| Reset a user's password | ✅ | ❌ |
| Progress dashboard | ✅ | ❌ |
| Corpus view (browse + read) | ✅ (+ per-member filter) | ✅ (whole corpus) |
| LLM correct / translate / vision | ✅ (own key) | ✅ (own key) |

Enforcement is server-side (`app/main.py` + `pipeline/db/assignments_repo.py`); the
UI mirrors it (hidden controls, read-only editor, disabled buttons).

---

## 7. End-to-end workflow

1. **Admin creates reviewer accounts** (👑 panel → Create reviewer) and shares
   credentials.
2. **Admin uploads a PDF** → a job appears (`pending → running → done` as the worker
   OCRs it). Split very large books into ~50-page PDFs for speed/reliability.
3. **Admin assigns page ranges** (👑 panel → Assign a page range: reviewer, job #,
   from/to pages).
4. **Reviewers log in**, open their job, and fix + verify entries — using the re-OCR,
   AI-correct, and 👁 vision helpers (with their own LLM key) as needed. They set
   **Trang số** to the real book page where relevant.
5. **Track** via the progress dashboard; **browse** finished work in 📚 Corpus.
6. **Export** each job's `.jsonl` (regenerated from the DB; spanning entries merged).

---

## 8. API reference

All `/jobs/*`, `/admin/*`, `/corpus/*`, `/pages/*`, `/llm/*` routes require a session.
Public: `/`, `/healthz`, `/auth/login`, `/auth/logout`, static assets.

**Auth**
- `POST /auth/login` `{username,password}` → sets session cookie
- `POST /auth/logout` · `GET /auth/me` → user + assignments

**Jobs & records**
- `POST /upload` (admin) — enqueue an extract job
- `GET /jobs` — list (reviewers: assigned only) · `GET /jobs/{id}` · `DELETE /jobs/{id}` (admin)
- `GET /jobs/{id}/records` — records + per-record `editable` flag
- `GET /jobs/{id}/output` — JSONL (merged, regenerated from DB)
- `POST /jobs/{id}/record` (edit/verify) · `/record/new` · `/record/delete`
- `POST /jobs/{id}/record/link` · `/record/unlink` — spanning-entry links
- `POST /jobs/{id}/reocr` + `GET /reocr/{rid}` — re-OCR a box
- `POST /jobs/{id}/autoscan_page` — AI auto-scan one page → pending records (BYO key)
- `POST /jobs/{id}/autoscan_batch` — queue a Gemini Batch scan of unverified pages (BYO key)
- `POST /jobs/{id}/autoscan_batch/status` — poll a batch; auto-applies results on success
- `GET /jobs/{id}/autoscan_batch` — list a job's batch scans (resume UI)
- `GET /pages/{filename}` — a rendered page image

**LLM (bring-your-own-key)**
- `GET /llm/providers` — names + default model + `supports_vision`
- `POST /jobs/{id}/correct` · `/translate` · `/vision_correct` — `{id, provider, api_key, model, …}`

**Admin**
- `POST /admin/users` · `GET /admin/users` · `POST /admin/users/{id}/password`
- `POST /admin/assignments` · `GET /admin/assignments` · `DELETE /admin/assignments/{id}`
- `GET /admin/progress`

**Corpus**
- `GET /corpus/pages?member=&offset=&limit=` · `GET /corpus/summary?member=` · `GET /corpus/page/{page}`
- `POST /corpus/sync-sheet` (admin) — push the verified corpus to Google Sheets

---

## 9. Configuration (env vars)

Set in `.env` (gitignored; template in `.env.example`). `docker-compose.yml` builds
`DATABASE_URL` from the `POSTGRES_*` values.

| Var | Purpose |
|---|---|
| `POSTGRES_USER/PASSWORD/DB` | DB credentials |
| `DATABASE_URL` | Postgres DSN (auto-built in compose) |
| `AUTH_SECRET` | **required** — signs session JWTs (empty = login disabled) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | seed the first admin (empty pw = no admin) |
| `COOKIE_SECURE` | `1` when served over HTTPS (else `0` for plain-HTTP/IP testing) |
| `OCR_BACKEND` | `paddle` (default) / `vision` |
| `OCR_USE_GPU` | `0` CPU (default) / `1` GPU |
| `OCR_LANG` | `chinese_cht` (Traditional — best for Sino-Nom) |
| `PDF_DPI` | render DPI (default 300; lower = faster/smaller, less accurate) |
| `TRANSLATE_BACKEND` / `CORRECT_BACKEND` | **`skip`** for this setup (per-user LLM keys do this in the app; `api` would demand a server `GOOGLE_API_KEY`) |
| `WORKER_POLL_S` | queue poll interval (default 2s) |
| `GOOGLE_SHEET_ID` | target spreadsheet id for the Corpus "Sync sheet" export (optional) |
| `GOOGLE_SHEETS_CREDENTIALS_B64` | service-account JSON, base64-encoded (or `GOOGLE_SHEETS_CREDENTIALS_FILE` = mounted path) |

> The server needs **no** LLM API keys — vision/correct/translate use each reviewer's
> own key from the browser.

### Google Sheets export setup (optional)

Enables the admin **☁ Sync sheet** button in the Corpus tab (§5). One-time:

1. **Google Cloud** → create/pick a project → **enable the Google Sheets API** →
   create a **service account** → create a **JSON key** and download it.
2. Create a Google Sheet → **Share** it as **Editor** with the service account's
   email (looks like `name@project.iam.gserviceaccount.com`).
3. Copy the spreadsheet id from its URL (`.../spreadsheets/d/<ID>/edit`) and set:
   - `GOOGLE_SHEET_ID=<ID>`
   - `GOOGLE_SHEETS_CREDENTIALS_B64=$(base64 -w0 service-account.json)`
4. `docker compose up -d --build app` (new `gspread` dependency → rebuild).

Export is **admin-only**, **verified entries only**, and a **full replace** of the
two tabs on every sync (safe to re-run). The service-account JSON is read from env,
never committed or logged.

---

## 10. Deployment

Full runbook in **`docs/DEPLOYMENT.md`**. Summary (single VM, e.g. Contabo):

1. Point a domain's A record at the VM. Open 22/80/443 (UFW).
2. Install Docker; clone the repo; `cd hannom-app`.
3. `cp .env.example .env`, set strong `POSTGRES_PASSWORD`, `AUTH_SECRET`,
   `ADMIN_PASSWORD`, and `TRANSLATE_BACKEND=skip`, `COOKIE_SECURE=1`.
4. Bind the app to localhost (`ports: "127.0.0.1:8000:8000"`) and put **Caddy** in
   front (`your.domain { reverse_proxy 127.0.0.1:8000 }`) for automatic HTTPS.
5. `docker compose up -d --build` (worker image build ~10–20 min).
6. Log in as `admin`; create reviewers + assignments.

**Sizing** (3–4 reviewers, ~2,400 pages): 4–8 vCPU / 8–16 GB / ~30 GB SSD; page
images ≈ 2 MB/page. OCR is the one-time cost (~10–30 s/page CPU); scale workers or
use a GPU to speed it up.

**Update**: `git pull && docker compose restart app worker` (bind-mounted code); use
`up -d --build` only when dependencies/Dockerfiles change.

---

## 11. Operations & troubleshooting

- **Backups (do this)** — the DB is the irreplaceable corpus:
  `docker compose exec -T postgres pg_dump -U hannom hannom | gzip > backup.sql.gz`
  (nightly cron), kept off-box. Also back up `data/uploads/`.
- **Worker crash-loops with "Missing GOOGLE_API_KEY"** → set `TRANSLATE_BACKEND=skip`
  in `.env` and `docker compose up -d` (not `restart` — env needs a recreate).
- **Job stuck "pending"** → the worker isn't ready: `docker compose logs worker`.
  First run downloads PaddleOCR models (wait a few min); a traceback = crash;
  `Killed`/OOM = too little RAM.
- **Job "failed" on one page** → fixed: bad pages are skipped now; re-upload after a
  `git pull`. The log lists skipped pages.
- **Inspect the DB with DBeaver** — Postgres is published on `127.0.0.1:5432`
  (host `localhost`, db/user `hannom`, password from `.env`).
- **Forgot admin password** → read `.env` (`grep ADMIN_PASSWORD .env`) or reset via
  `docker compose exec app python -c "..."` bcrypt update (see §chat history), or an
  admin can reset any user from the Users table.
- **Scale OCR** → `docker compose up -d --scale worker=3` (add cores first).

---

## 12. Development

- **Repo layout**: `pipeline/` (shared: layouts, ocr, db, llm, runner, schema),
  `app/` (FastAPI + static UI), `worker/` (queue loop), `tests/`, `docs/`.
- **Tests**: `pytest` (31 tests — two_column extraction, jobstore, registries,
  translate, quality/regression). Run: `pytest -o addopts=""` (repo pins `--cov`).
  Tests use the SQLite store + mock fixtures, so **no Postgres/OCR needed**.
- **Two Python envs** (heavy OCR deps don't fit one): app deps in
  `requirements-app.txt` (fastapi, uvicorn, psycopg, PyJWT, bcrypt, LLM SDKs,
  httpx pinned); worker deps in `requirements-worker*.txt` (paddleocr, paddlepaddle
  CPU/GPU, pdfplumber, pdf2image, psycopg).
- **LLM providers** (`pipeline/llm/`): `base.py` protocol (`complete` +
  `complete_vision` + `supports_vision`), `__init__.py` registry, one file per
  vendor. Add a provider = new file + one `register(...)` call.

---

## 13. Known limitations & notes

- **Trang số uniqueness** — the Corpus view keys pages on `records.page` (Trang số).
  If uploads keep their default per-PDF page numbers, every PDF's "page 1" collides
  under corpus page 1. Set Trang số to the real book page for a clean corpus (the
  page list shows contributing uploads so collisions are visible).
- **Corpus is open to all** reviewers (read-only, whole corpus) by design; jobs are
  scoped to assignments but the finished corpus browse is shared.
- **File size** — no hard upload cap; a very large PDF loads into app memory on
  upload and takes ~hours to OCR. Split big books into ~50-page chunks.
- **OCR quality** — clean typeset pages read well after watermark removal; residual
  character errors are cleaned via re-OCR or the LLM helpers during review.
- **Spanning-entry linking** is manual (reviewer marks the continuation) and links to
  the *previous* entry; verify the direction if an entry starts on the later page.
- **GPU** is optional; CPU works everywhere but is the throughput bottleneck for the
  one-time bulk OCR.
