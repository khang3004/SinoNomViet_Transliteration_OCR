# Migration plan: multi-user + Supabase + bring-your-own-key LLM

Status: **design doc for review — no code changes yet.**

Goal: turn the current single-user local app into a **multi-user** service where
each user logs in, their data is isolated, and they **paste their own LLM API
key** (Gemini / OpenAI / …) for translation & correction.

---

## 0. The one thing to get straight first

**Supabase does NOT run your OCR.** It is backend-as-a-service (Postgres, Auth,
Storage, Realtime) — not compute for PaddleOCR. So:

- **The `worker` stays** a separate process on a CPU/GPU host. It stops using
  local SQLite + files and instead **talks to Supabase** (Postgres for the job
  queue, Storage for files) via a service-role key.
- Supabase replaces the **app's** storage/DB/auth layer, not the ML.

```
            ┌────────── Supabase ──────────┐
 Browser ──▶│ Auth · Postgres(RLS) · Storage│◀── worker (PaddleOCR, GPU/CPU)
   ▲        └──────────────────────────────┘        on a VM / Render / Fly
   │                    ▲
   └──── FastAPI app (JWT-verified API + LLM proxy) ┘
```

---

## 1. What maps to what

| Today | → After |
|---|---|
| SQLite `data/jobs.db` (`pipeline/jobstore`) | **Postgres** tables `jobs`, `records` |
| JSONL in `data/output/*.jsonl` | **`records`** rows (one row per parallel unit) |
| `data/uploads/*`, `data/output/pages/*` | **Storage** buckets `uploads`, `pages` |
| no auth | **Supabase Auth** → `user_id` (uuid) on every row |
| polling `/jobs`, `/reocr`, `/correct` | keep polling, or **Realtime** subscriptions |
| keys in worker env (`GOOGLE_API_KEY`) | **per-user key, pasted in UI, per-request** |
| `OCR_BACKEND` etc. (env, global) | OCR still global on the worker; **LLM per-user** |

The project's **registry pattern + decoupled worker + `jobstore` interface** are
exactly the seams that make this tractable. The `JobStore` class is the clean
swap point (SQLite → Postgres) — same methods, new backend.

---

## 2. Database schema (Postgres)

```sql
-- Supabase provides auth.users. A profile row per user (optional extras).
create table profiles (
  id         uuid primary key references auth.users on delete cascade,
  created_at timestamptz not null default now()
);

create table jobs (
  id           bigint generated always as identity primary key,
  user_id      uuid   not null references auth.users on delete cascade,
  kind         text   not null default 'extract',   -- extract | reocr
  status       text   not null default 'pending',   -- pending|running|done|failed
  filename     text   not null default '',
  source_doc   text   not null default '',
  storage_path text   not null default '',          -- input file key in Storage
  payload      jsonb  not null default '{}',         -- reocr bbox/page, etc.
  result       jsonb,                                -- small results (reocr text)
  error        text   not null default '',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index on jobs (status, id);
create index on jobs (user_id);

create table records (
  id           uuid primary key default gen_random_uuid(),
  human_id     text not null,                        -- e.g. "HVB_001.001.01"
  job_id       bigint not null references jobs on delete cascade,
  user_id      uuid   not null references auth.users on delete cascade,
  page         int, line_no int, entry_no int,
  han          text not null default '',
  han_raw      text not null default '',             -- raw OCR before correction
  phonetic     text not null default '',
  meaning      text not null default '',
  entry_meta   jsonb not null default '{}',
  layout_type  text not null default 'two_column',
  image_path   text not null default '',             -- page image key in Storage
  han_bbox     jsonb, meaning_bbox jsonb,
  source_of    jsonb not null default '{}',
  review_status text not null default 'pending',     -- pending|verified
  reviewed_by  uuid, reviewed_at timestamptz,
  created_at   timestamptz not null default now()
);
create index on records (job_id);
create unique index on records (user_id, human_id);
```

Notes:
- `records.id` is a uuid (the old `HVB_001.001.01` becomes `human_id`, unique
  *per user*). This avoids cross-user id collisions.
- The current `Record` dataclass (`pipeline/schema.py`) maps 1:1 to columns —
  `to_dict()` already produces this shape.

---

## 3. Row-Level Security (per-user isolation)

```sql
alter table jobs    enable row level security;
alter table records enable row level security;
alter table profiles enable row level security;

create policy "own jobs"    on jobs    for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own records" on records for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own profile" on profiles for all
  using (auth.uid() = id) with check (auth.uid() = id);
```

- A logged-in user (anon/JWT) can only see/modify **their** rows — enforced by
  the database, not app code.
- The **worker** uses the **service-role key**, which *bypasses RLS* (it must
  process all users' jobs). So the worker code must be careful: always carry
  `user_id` from the claimed job onto the records it writes, and never mix users.

---

## 4. Storage

Two buckets, both **private** (served via signed URLs):
- `uploads/{user_id}/{job_id}/{filename}` — the source PDF/image.
- `pages/{user_id}/{job_id}/p{NNNN}.png` — rendered page images for the review UI.

Storage RLS policies restrict each path prefix to its owner. The app issues
short-lived **signed URLs** for the review UI to load page images.

Caveat: page PNGs are ~1–2 MB each at 300 dpi → watch Storage usage/cost; consider
JPEG or a lower dpi for the *display* image (keep 300 dpi only for OCR).

---

## 5. The crux: per-user API keys (bring-your-own)

**Recommended pattern — do NOT store keys server-side by default:**

1. User pastes their key in the UI (a field with provider dropdown: Gemini /
   OpenAI / Anthropic).
2. The key is held **client-side** (in-memory or `sessionStorage`) and sent
   **per request** to the app on the calls that need it (translate / correct).
3. The app uses it for that one call and **discards it** — never persisted,
   never logged. (We already log key *presence* only; keep that discipline.)

**Where the LLM call runs changes for multi-user:**
- **OCR** must stay on the worker (needs the model). No keys there.
- **Translation / correction are plain HTTPS calls** — move them to the **app**,
  executed synchronously with the user's per-request key. This keeps keys out of
  the Postgres job queue and off the worker entirely.
- ⇒ The `kind="correct"` *worker* job I just added would be **replaced** by an
  app-side `POST /correct` that takes `{provider, api_key, text, context}` and
  calls the provider directly. (Same UX, better trust boundary.)

**If you must persist keys** (convenience): use **Supabase Vault** (`pgsodium`,
encrypted at rest), one secret per user, decrypted only in a SECURITY DEFINER
function the worker/app calls. Document the added liability of custodying keys.

---

## 6. Multi-provider LLM (Gemini / OpenAI / Anthropic)

Fits the existing registry pattern. A provider exposes:

```python
class LLMProvider(Protocol):
    name: str
    def complete(self, prompt: str, api_key: str, model: str | None = None) -> str: ...
```

- `pipeline/llm/gemini.py`, `openai.py`, `anthropic.py` — each a thin client.
- `translate` and `correct` become **prompt builders** on top of a chosen
  provider, taking `(text, context, provider, api_key, model)`.
- The current `pipeline/correct/api.py` + `pipeline/translate/gemini.py` collapse
  into "provider=gemini" of this scheme; the Vietnamese-context prompt we just
  added carries over.

---

## 7. Worker wiring (SQLite → Postgres)

Implement `PostgresJobStore` behind the **existing `JobStore` interface**
(`create / claim_next / mark_done / mark_failed / get / list_jobs /
requeue_running`). Backend: `psycopg`/`supabase-py` with the service-role key.

- `claim_next`: `UPDATE jobs SET status='running' WHERE id = (SELECT id FROM jobs
  WHERE status='pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *;`
  → **`SKIP LOCKED` enables multiple concurrent workers** (a real upgrade over
  SQLite's single-worker model).
- Worker per job: download input from Storage → temp file → run pipeline →
  upload page PNGs to Storage → bulk-insert `records` rows (carrying `user_id`).
- `pipeline/runner.py` changes only at the I/O edges (read input / write records
  / save page images) — the extraction logic is untouched.

Config: worker gets `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (env, worker
only) instead of `DATA_DIR`.

---

## 8. App + frontend

Lowest-rewrite path: **keep FastAPI as the API gateway.**
- Add Supabase **Auth**: the browser logs in with `supabase-js`; every request to
  FastAPI carries the Supabase **JWT**; FastAPI verifies it (Supabase JWT secret)
  and extracts `user_id`.
- FastAPI queries Postgres scoped to `user_id` (or uses the user's JWT against
  PostgREST so RLS applies).
- Endpoints stay (`/upload`, `/jobs`, `/jobs/{id}/records`, `/record`, `/reocr`,
  + new `/correct`, `/translate` with per-request key). `/upload` writes to
  Storage and inserts a `jobs` row.
- Frontend: add a **login screen** + an **API-key/provider field**; the review
  editor becomes per-user (unchanged otherwise). Page images load via signed URLs.

(Alternative, more rewrite: browser talks to Supabase directly via PostgREST/
Realtime, FastAPI only for upload + LLM proxy. More "Supabase-native," bigger
frontend change. Recommend the gateway approach first.)

---

## 9. Phased rollout (each phase shippable)

1. **Per-user-key + multi-provider** (`pipeline/llm/*`, app `/correct`+`/translate`
   take a key, drop the worker `correct` job). *Independent of Supabase; de-risks
   the biggest behavioral change.*
2. **PostgresJobStore + records table** behind the `JobStore` interface; worker +
   app point at Postgres. Keep local Storage for now.
3. **Auth + RLS**: Supabase Auth, `user_id` columns/policies, JWT verification in
   FastAPI.
4. **Storage**: move uploads + page images to Supabase Storage + signed URLs.
5. **Frontend**: login + per-user key UI; (optional) Realtime instead of polling.

---

## 10. Open decisions (yours)

- **Worker hosting** — where does the GPU/CPU worker run (a VM, Render, Fly,
  your 2060 box)? Supabase can't host it.
- **Persist keys or not** — pasted-per-session (recommended) vs Vault-encrypted.
- **Which providers** at launch (Gemini + OpenAI? + Anthropic?).
- **Gateway vs Supabase-native frontend** (see §8).
- **Cost** — Storage for page images, DB rows per corpus; free tier limits.

---

## 11. What does NOT change

- The extraction pipeline (`pipeline/layouts/two_column.py`, the TRÍCH YẾU /
  metadata / bbox logic), the OCR engines, and the `Record` schema shape.
- The registry pattern (OCR / layouts / translate / correct) — it gains a
  provider dimension but the structure holds.
- The review editor UX (boxes, re-OCR, AI-correct) — it just becomes multi-user.
