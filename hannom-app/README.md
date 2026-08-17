# Han Scanner

A stage in the Facebook crawl pipeline. It answers one question per post —
**does this image actually contain Han/CJK text?** — so the expensive Gemini
stage only ever sees worthwhile posts.

```
crawl Facebook → MinIO → [Han Scanner] → Gemini batch (boxing + OCR)
```

This app runs on its own VPS and serves the images it keeps back to Gemini over
HTTPS.

---

## Two ways to feed it

**Upload (default, no MinIO needed).** The normal loop:

1. Upload the crawler's `valid_post.jsonl` from the dashboard
2. Process — repeat until nothing is pending
3. Download the result and hand it to the Gemini stage

Step 2 offers two paths:

| | What it does | Cost |
|---|---|---|
| **Prepare URLs only** | Downloads each image, issues a signed URL on this domain. Output: `ready_for_ocr.jsonl`, **no verdict**. | Network-bound, minutes |
| **Prepare + OCR here** | Also runs PP-OCRv6 locally, splitting into `han_valid` / `han_invalid`. | CPU-bound, hours |

Prepare-only is usually the right choice: the next stage runs Gemini over these
images anyway, so scanning here is often redundant work on a 4-vCPU box.

**"Skipped" is not "no Han text".** Unscanned records carry `scan_status:
"skipped"` and `han_valid: null` — never `false`. A consumer that treats null as
false is making its own mistake rather than inheriting a lie from this stage.

That export is **cumulative**, so re-uploading it after a fresh crawl is
expected. A local checkpoint (`data/state/processed_ids.jsonl`) keyed on
`post_id` means the second upload only scans posts that are actually new.

**MinIO (optional).** If `MINIO_ENDPOINT` and `MINIO_GROUP_PREFIX` are both set,
the scanner can also read the crawler's `logs/by_run/*/upserts.jsonl` directly
and mirror results back. A scheduler then keeps pace with the crawl automatically.

Everything MinIO stays dormant unless configured — the app starts and runs
normally without it, and simply hides those parts of the UI. Results are always
written locally regardless; MinIO is a mirror, never the only copy.

---

## How it works

Each batch runs four phases in a fixed order:

```
preflight  decode every signed-URL expiry     → gate
download   fetch from the Facebook CDN        → minutes, high concurrency
ocr        PP-OCRv6 over local files          → hours, parallel, resumable
publish    verdicts + errors back to MinIO
```

**The order is forced by expiry.** fbcdn signs image URLs with a lifetime in
hours, while OCR of a full corpus takes hours. Downloading everything first
(network-bound, minutes) and only then scanning (CPU-bound, hours) is what keeps
the last image as valid as the first. Interleaving them would fetch the final
image long after its signature died.

Preflight is a **gate, not a log line**: if more than 10% of URLs are already
dead, the batch pauses for a human. A stale crawl is worth re-running, not
scanning for six hours to produce failures.

### Continuous, not one big job

Scanning starts right after crawling, so the app keeps pace with the crawl
rather than running once. A scheduler claims `BATCH_SIZE` (default 500) unscanned
posts every `SCAN_INTERVAL`. Small batches mean failures cost minutes, progress
is always visible, and images are fetched while their URLs are fresh.

---

## Where things land

**Local (always).** Under `DATA_DIR`, mirroring the MinIO layout exactly, so a
file produced either way is interchangeable to the Gemini stage:

| Path | Contents |
|---|---|
| `results/export/ready_for_ocr.jsonl` | Downloaded and signed, **not scanned** → feeds Gemini |
| `results/export/han_valid.jsonl` | Scanned, has Han text |
| `results/export/han_invalid.jsonl` | Scanned, clean |
| `results/errors/failed.jsonl` | Cumulative failures, for retry sweeps |
| `results/logs/by_run/<run>/…` | Per-run `result.json`, `upserts.jsonl`, `errors.jsonl` |
| `state/processed_ids.jsonl` | The checkpoint |
| `uploads/<id>/valid_post.jsonl` | Uploaded exports |
| `images/` | Downloaded images, served to Gemini |

All three export files are downloadable from the dashboard's **Data** tab.

## MinIO layout (only when configured)

Bucket `final-exam-nlp-raw`, group prefix `facebook/<group_id>/`.

**Read**

| Path | Role |
|---|---|
| `logs/by_run/<run_id>/upserts.jsonl` | Work source. Holds **both** crawler-valid and crawler-invalid posts, so records are filtered on `is_valid` and deduped on `post_id` (it is an *upsert* log — the same post recurs across runs). |
| `export/valid_post.jsonl` | Line count only, as the progress denominator. |

**Write** — a `han_scan/` namespace mirroring the crawler's own convention:

| Path | Contents |
|---|---|
| `han_scan/export/han_valid.jsonl` | Has Han text → **feeds Gemini** |
| `han_scan/export/han_invalid.jsonl` | Scanned clean |
| `han_scan/errors/failed.jsonl` | Cumulative failures, for retry sweeps |
| `han_scan/logs/by_run/<run>/result.json` | Run summary |
| `han_scan/logs/by_run/<run>/upserts.jsonl` | Records touched this run |
| `han_scan/logs/by_run/<run>/errors.jsonl` | Failures from this run only |
| `han_scan/state/processed_ids.jsonl` | Idempotency index |

Failures land in two places deliberately: per-run (what broke during that run)
and cumulative (current state of every failure).

---

## Output contract

`han_scan/export/han_valid.jsonl`, one object per post:

```jsonc
{
  "post_id": "...", "group_id": "...", "post_link": "...", "author": "...",
  "scan_status": "scanned",     // "scanned" | "skipped"
  "han_valid": true,            // true if ANY image has Han text; null when skipped
  "han_words_total": 34,
  "images_scanned": 2, "images_failed": 0,
  "images": [{
    "url": "https://<domain>/img/<post_id>/0.jpg?exp=...&sig=...",  // Gemini fetches this
    "idx": 0, "width": 1170, "height": 1461, "bytes": 284113,
    "content_type": "image/jpeg", "sha256": "...",
    "source_url": "https://scontent....fbcdn.net/...",
    "source_expires_at": "2026-08-16T03:06:59+00:00",
    "url_expires_at": "2026-09-15T...", "downloaded_at": "...",
    "valid_pic": true, "han_words": 34, "boxes": 7,   // all null when skipped
    "texts": ["..."], "mean_confidence": 0.94, "scan_ms": 380
  }],
  "source_key": "...", "source_run_id": "...", "scan_run_id": "...",
  "stage": "han_scan", "schema_version": "han_scan/1.1",
  "ocr_engine": "paddleocr-3.7.0:PP-OCRv6", "scanned_at": "...",
  "label": "...", "sub_caption": "...", "posted_at": null
}
```

Two things worth knowing:

- **`han_valid`, not `is_valid`.** The input already uses `is_valid` for the
  *crawler's* verdict (did the crawl match?). Ours is a different question, so it
  gets a different name — overloading it would corrupt meaning downstream.
- **Every image is scanned**, not just `images[0]`, and `han_valid` is true if any
  of them has Han text.

Errors carry an `error_class` and a `retryable` flag, so a retry sweep skips
permanently dead links (`expired_url`, `http_404`) and only re-runs transient
ones (`timeout`, `http_429`, `http_5xx`).

---

## Images are served from this VPS

The Gemini stage uses **URLs from our domain**, and Gemini fetches them
anonymously. So images stay on local disk and are served by this app.

That collides with the app being behind a login on a public domain. The
resolution: `/img/*` is the only unauthenticated route, guarded by an **HMAC
signature** instead of a session. Only URLs this service minted will serve, and
they expire after `IMAGE_URL_TTL_DAYS` (default 30 — a TTL shorter than the
downstream Gemini run is a silent failure at the last step).

Nothing auto-deletes images: deleting one whose source URL has expired is
unrecoverable.

---

## Running it

```bash
cp .env.example .env      # then fill it in — the app refuses to start without AUTH_SECRET
docker compose up --build -d
docker compose logs -f scanner
```

Generate the two secrets and the password hash:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```bash
docker compose run --rm scanner python -c "from app.api.auth import hash_password; print(hash_password('YOUR-PASSWORD'))"
```

The app binds `127.0.0.1:8000`; put a reverse proxy in front for the public
domain. `--workers 1` is **required** — batch state lives in the process, and a
second uvicorn worker would fork it and corrupt progress tracking.

### Headless

The core pipeline has no web dependency, so it also runs without the app:

```bash
docker compose run --rm scanner python -m app.cli --check
```

```bash
docker compose run --rm scanner python -m app.cli --preflight-only --file /data/uploads/<id>/valid_post.jsonl
```

```bash
docker compose run --rm scanner python -m app.cli --file /data/uploads/<id>/valid_post.jsonl --limit 100
```

Add `--ocr` to also scan locally; without it the CLI prepares URLs only, matching
the dashboard's default.

Omit `--file` to use the most recent upload; add `--minio` to read the crawler's
by_run logs instead. `--check` reports configuration and upload status and does
**not** fail when MinIO is absent — that's the normal mode.

---

## Architecture

```
app/
  core/     the pipeline — NO web imports, ever
            models  parser  source  sink  storage  downloader
            ocr  batch  jobstore  scheduler  signing  imagestore  health
  api/      the HTTP adapter — FastAPI lives only here
  cli.py    headless runner (proves core is genuinely decoupled)
```

`core` talks to two protocols, so the crawl team can swap either side without
touching scanner logic:

```python
class RecordSource(Protocol):
    def iter_pending(self, limit: int) -> PendingBatch: ...
    def mark_done(self, post_ids: list[str], scan_run_id: str) -> None: ...

class ResultSink(Protocol):
    def write_results(self, records: list[HanScanRecord], scan_run_id: str) -> None: ...
    def write_errors(self, errors: list[HanScanError], scan_run_id: str) -> None: ...
```

Shipped implementations: `FileRecordSource` / `MinioRecordSource`, and
`FileResultSink` / `MinioResultSink` / `TeeResultSink` (local plus mirror). The
upload flow exists because those protocols did — it was an implementation, not a
redesign.

**Unknown record shapes:** `core/parser.py` ships a `RecordParser` protocol with
a `CustomRecordParser` stub. Implement `parse()` there for a schema the default
does not handle; nothing else changes.

### Fault tolerance

- Every terminal state is fsynced as it happens — append-only logs, nothing
  memory-only. Resume replays them and skips finished work.
- **Results publish during the run, not only at the end.** Finished posts are
  written and checkpointed after each OCR chunk, so stopping a multi-hour scan
  costs one chunk rather than everything scanned so far. A post is published only
  once *all* its images are resolved — a half-scanned post would carry a
  `han_valid` computed from part of the evidence, so it stays unpublished and
  gets reclaimed whole later.
- **Images already on disk are never re-downloaded.** An interrupted run costs no
  bandwidth to redo, which matters because those signed CDN URLs may have expired
  in the meantime.
- A poison image cannot kill a run: `BrokenProcessPool` is caught, the pool is
  rebuilt, and the chunk is retried serially so the bad image is attributed to
  itself. Verified against workers that hard-exit mid-batch.
- A per-image timeout stops one pathological file stalling a worker.
- The memory guard sheds OCR workers before the OOM killer fires — on 8 GB with
  3 workers at ~1 GB each, that is the difference between a slow batch and a dead
  one at hour four.
- Jobs interrupted by a container restart are detected via a stale heartbeat and
  offered for resume rather than appearing to run forever.

---

## Tuning

Host reference: 4 vCPU / 8 GB / 100 GB SSD / 200 Mbit.

| Phase | Per 500-batch | Full 20k |
|---|---|---|
| Download | ~10–20 s | 3–13 min |
| OCR (3 workers) | benchmark it | benchmark it |
| Disk | ~0.5 GB | 4–19 GB of 100 GB |

**RAM is the binding constraint, not CPU.** Before a full run, benchmark
`OCR_WORKERS` at 2/3/4 with `--limit 100` and watch `docker stats`. PP-OCRv6
claims a large CPU speedup over v5 and ships a compact unified model, but
measure it on your images rather than trusting the number.

The monitoring UI charts RAM, CPU, disk, and per-worker RSS live — those are the
numbers that predict failure on this box.

---

## Tests

The suite deliberately needs none of the heavy runtime stack, so it runs anywhere
in seconds:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

What it does **not** cover, by design, is anything requiring real I/O — MinIO,
PaddleOCR, live HTTP. Verify those on the VPS with `app.cli --check`,
`--preflight-only`, and a `--limit 100` run.
