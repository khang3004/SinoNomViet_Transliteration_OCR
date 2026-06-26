# hannom-app — Han-Nom OCR corpus builder

Builds a parallel **JSONL** corpus (Han · phonetic · Vietnamese meaning ·
metadata) from Han-Nom documents, for training MT models. Self-contained under
`hannom-app/`; nothing outside this directory is modified.

The design is **extensible by registry**: a new document layout or a new OCR
engine is added by dropping one file in and making a single `register(...)`
call — no existing handler is touched.

> Primary document type: **"Mục lục Châu bản triều Nguyễn"** (`two_column`),
> built fully. Secondary/future: `three_block` (Ức Trai Tập) and `han_only`
> (ported from the existing vertical-column engine, output unchanged).

---

## Architecture

Two decoupled services + a shared `data/` volume (AGENTS.md §3):

```
 upload (web UI) → app (FastAPI, no GPU) → data/jobs.db ← worker (OCR on GPU)
                        ▲  reads JSONL          uploads/      extract → JSONL
                        └───────────────────────output/ ◄──────────┘
```

- **app**: lightweight, no GPU, no API keys. Serves the UI, accepts uploads,
  enqueues jobs, serves JSONL output.
- **worker**: needs the GPU for OCR. Reads API keys from env (worker only).
- **SQLite job queue** (`data/jobs.db`) decouples the two. Containers are
  stateless; all state is in the mounted `data/` volume.

### Two registries (the core requirement)

| Registry | Package | Built-ins | Select via |
| --- | --- | --- | --- |
| OCR engines | `pipeline/ocr/` | `paddle` (default), `vision`, `kandianguji` (stub), `mock` | `OCR_BACKEND` |
| Layout handlers | `pipeline/layouts/` | `two_column` (primary), `three_block`, `han_only` | router (priority order) |

Add an engine: new file in `pipeline/ocr/` + `register("name", Cls)`.
Add a layout: new file in `pipeline/layouts/` + `register(Handler())`.
The router tries layouts in priority order; `two_column` is checked first.

---

## Quick start — test locally (no GPU, no Paddle, no real PDF)

From `hannom-app/`:

```bash
# 1. PRIMARY two_column extraction on MOCK PDF text + MOCK Han OCR.
#    Proves han↔meaning pairing by y, entry_meta parsing, watermark filtering.
python -m scripts.dryrun_two_column

# 2. Regression: ported han_only/three_block reproduce the ORIGINAL src/ engine.
python -m scripts.dryrun_three_block

# 3. Show both registries + single-call extensibility + secret-safe startup.
python -m scripts.show_registries

# 4. Unit tests (needs pytest):  pip install pytest && python -m pytest
```

### Run the full app + worker locally (still no GPU)

The `mock` OCR backend lets the whole stack run end-to-end on the sample image:

```bash
pip install -r requirements-app.txt        # app deps only

# terminal A — web UI
DATA_DIR=./data python -m uvicorn app.main:app --port 8000
# open http://localhost:8000  and upload tests/assests/image.png

# terminal B — worker with the GPU-free mock engine
OCR_BACKEND=mock TRANSLATE_BACKEND=skip DATA_DIR=./data python -m worker.worker
```

The worker processes the upload (an image has no text layer, so the router falls
back to `han_only` full-OCR) and writes JSONL you can download from the UI.

**See the full `two_column` extraction in the browser** without any upload: click
**▶ Run two_column demo** on the home page (or `GET /demo/two_column`). It runs
the PRIMARY handler on synthetic mock data and renders the parallel
Han↔Vietnamese records with parsed metadata — no GPU, no PDF. Completed upload
jobs also have a **view** link that renders their records inline.

### Share it with a friend (public URL, no GPU)

Want someone else to upload images and try it? See **[SHARE.md](SHARE.md)** — it
sets up CPU PaddleOCR + a free cloudflared tunnel so you get a public
`https://…trycloudflare.com` URL in a few minutes (live while your PC runs).
Note: image uploads run `han_only` OCR; the full `two_column` pairing needs a
text-layer PDF or the in-app demo.

### Run with Docker Compose

```bash
cp .env.example .env        # fill GOOGLE_API_KEY for Gemini translation
docker compose up --build   # app on :8000 + worker
```

Uncomment the GPU block in `docker-compose.yml` on a GPU host to run PaddleOCR
on the GTX 2060.

---

## Configuration (env-driven, 12-factor — AGENTS.md §6)

| Env | Values | Default | Notes |
| --- | --- | --- | --- |
| `OCR_BACKEND` | paddle / vision / kandianguji / mock | `paddle` | paddle fits the 2060 |
| `TRANSLATE_BACKEND` | api / offline / mock / skip | `api` | api = Gemini flash (cheap) |
| `TRANSLATE_MODEL` | gemini model id | `gemini-2.0-flash` | used when backend=api |
| `CORRECT_BACKEND` | skip / api / offline | `skip` | |
| `QWEN_MODEL` | hf id | `Qwen2.5-3B-Instruct` | only if offline + bigger GPU |
| `DSG_FFF` | str | `HVB_001` | work id |
| `PDF_DPI` | int | `300` | Han crop render dpi |

Translation defaults to the **API** because the 6 GB 2060 cannot host OCR + a
3B LLM together; offline LLM translation stays behind a flag for bigger GPUs.
The runner fills empty `meaning` fields via the selected translator (registry in
`pipeline/translate/`: `api`=Gemini, `offline`=Qwen stub, `mock`=key-free
placeholder, `skip`=no-op). Records that already carry a higher-trust meaning
(two_column's `pdf_text`) are never overwritten.

### Secrets (AGENTS.md §7)

- `GOOGLE_API_KEY` (Gemini) and `GOOGLE_VISION_KEY` are read from the
  **environment**, injected into the **worker only**. Never hardcoded, never
  logged — startup prints only a *present/absent boolean* per key.
- Local: keys live in `.env` (gitignored). Only `.env.example` is committed.
- The worker **fails fast** at start if a selected `*_BACKEND=api` is missing
  its key.

---

## JSONL schema (AGENTS.md §5)

One line = one paired Han/Vietnamese unit. Fields are additive; new ones default
to null/empty. For `two_column`, `meaning` comes from the PDF text layer
(`source_of.meaning="pdf_text"`, highest trust) and `phonetic` stays empty.
See `pipeline/schema.py`.

---

## PRIMARY layout: `two_column` (Châu bản) — hybrid extraction

The Vietnamese (right) side is a **real PDF text layer** (selectable,
watermark-free); the Han (left) side is **image-based**. So (AGENTS.md §4):

1. Vietnamese → extracted from the PDF text layer (`pdf_text.py`), no OCR.
2. Han → OCR of the left-column crop only.
3. Column split x derived from the span distribution (not hardcoded).
4. Right spans grouped into entries by leading entry numbers + y-bands.
5. Han ↔ Vietnamese paired per entry by **y-overlap**.
6. Per-entry metadata parsed (`Ngày:`/`Tờ/Tập:`/`Loại:`/`Xuất xứ:`/`Đề tài:`);
   `TRÍCH YẾU` and `Công đồng …:` headings dropped from the parallel body.
7. Han OCR post-filter drops low-confidence non-CJK tokens (watermark bleed).

### Coordinate spaces (real PDF)

The Vietnamese text layer comes back from pdfplumber in **PDF points** (72 dpi),
but the Han OCR runs on a page **raster rendered at `PDF_DPI`** (e.g. 300). So
`PageContext` scales the text spans by `PDF_DPI/72` into the raster's pixel space
and crops/OCRs the Han left column at the same dpi — both sides share one
coordinate system, so y-overlap pairing is exact. Rendering uses `pdf2image`
(poppler / `pdftoppm`).

> **Test-data note:** the repo has no real Châu bản PDF — only sample page
> images (no text layer). The two_column dry-run uses MOCK text spans + MOCK Han
> OCR (`tests/fixtures/mock_two_column.py`); the real PDF code path (scale +
> render + crop + OCR) is exercised by `tests/test_two_column_pdf.py` with the
> poppler render / text-layer / OCR calls monkeypatched. Validate against a
> genuine text-layer PDF (needs poppler + the OCR backend) when one is supplied —
> see the `TODO(real-pdf)` markers.

---

## Deployment readiness (AGENTS.md §8, §9)

No Kubernetes YAML is generated here. The app is deploy-ready because all config
and secrets are env-driven and all state lives in the mounted `data/` volume —
so a future K8s move maps directly to a **Deployment + Secret + PVC** with zero
code change.

For batch growth (scheduled re-runs, backfills), a future **Airflow** DAG can
enqueue jobs into the same `pipeline/jobstore` API the worker already consumes —
Airflow would orchestrate, the worker still executes. No Airflow code here.

---

## Layout

```
hannom-app/
├── app/                 FastAPI UI + upload/JSONL endpoints (no GPU, no keys)
│   ├── main.py
│   └── static/index.html
├── worker/worker.py     job loop: claim → run pipeline → write JSONL (GPU)
├── pipeline/
│   ├── config.py        env-driven config + secret-safe validation
│   ├── schema.py        JSONL Record / EntryMeta / SourceOf
│   ├── pdf_text.py      PDF text-layer spans + has_text_layer (+ mock seam)
│   ├── page_context.py  unit of work passed to handlers
│   ├── runner.py        file → records glue
│   ├── jobstore/        SQLite job queue (scheduler-friendly API)
│   ├── ocr/             OCR registry: base, paddle, vision, kandianguji, mock
│   └── layouts/         layout registry/router: two_column, three_block,
│                        han_only, _spatial (vendored existing engine)
├── scripts/             dryrun_two_column, dryrun_three_block, show_registries
├── tests/               pytest + fixtures (mock_two_column)
├── docker-compose.yml   local dev (app + worker, shared ./data, GPU block comm.)
├── Dockerfile.app / Dockerfile.worker
├── requirements-app.txt / requirements-worker.txt
└── .env.example         (.env is gitignored)
```
