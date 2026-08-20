# Image Prep

A stage in the Facebook crawl pipeline. It takes crawled post records, downloads
each image, and makes it fetchable from your own domain so the next stage can OCR
it:

```
crawl Facebook → MinIO → [Image Prep] → Gemini batch (boxing + OCR)
```

It makes **no claim about image contents**. An earlier version ran PaddleOCR here
to filter out images without Han text, but that filter cost hours of CPU on a
4-vCPU box to save work the Gemini stage does anyway. Dropping it took the
container from ~3 GB to a few hundred MB and the build from ~15 minutes to about
one. The history is in git if the filter is ever wanted back.

---

## Two ways to feed it

**Upload (works today, no MinIO needed):**

1. Upload the crawler's `valid_post.jsonl` from the dashboard
2. Prepare — repeat until nothing is pending
3. Download `ready_for_ocr.jsonl` and hand it to the Gemini stage

That export is **cumulative**, so re-uploading after a fresh crawl is expected. A
local checkpoint (`data/state/processed_ids.jsonl`) keyed on `post_id` means the
second upload only processes genuinely new posts.

**MinIO (optional):** with `MINIO_ENDPOINT` and `MINIO_GROUP_PREFIX` both set, it
reads the crawler's `logs/by_run/*/upserts.jsonl` directly and mirrors results
back, with a scheduler keeping pace automatically. Everything MinIO stays dormant
otherwise — the app runs normally without it and hides those parts of the UI.
Results are always written locally; MinIO is a mirror, never the only copy.

---

## How a batch works

```
preflight  decode every signed-URL expiry  →  gate
download   fetch from the Facebook CDN     →  minutes, high concurrency
publish    signed URLs + errors            →  written as posts finish
```

**Preflight is a gate, not a log line.** fbcdn signs image URLs with a lifetime
measured in hours. If more than 10% are already dead, the batch pauses for a
human — a stale crawl is worth re-running rather than downloading dead links.

**Publishing happens during the run.** Finished posts are written and
checkpointed as their images land, so stopping costs the in-flight items rather
than the batch. A post is published only once *all* its images are resolved.

**Images already on disk are never re-downloaded**, which makes an interrupted
run free to redo — and matters because those CDN URLs may have expired since.

---

## The admin gallery

The main page is a browser over what has been prepared: a grid of thumbnails,
each showing the author, caption and post id. Clicking one opens a lightbox with
dimensions, size, sha256, both expiry timestamps, the source CDN URL, a copy
button for the signed URL, and a link straight to the original Facebook post.
Arrow keys move between images in a multi-image post.

Search filters on author, post id, caption and post link — paste a Facebook URL
to find its post.

**Thumbnails re-sign on read.** A record written a month ago carries a signature
that has since expired; the gallery mints a fresh one so browsing never shows
broken images. The stored URL is what the Gemini stage consumes and is shown
alongside.

---

## Images are served from this VPS

The Gemini stage uses URLs from your domain and fetches them **anonymously**,
while the rest of the app sits behind a login. The resolution: `/img/*` is the
only unauthenticated route, guarded by an **HMAC signature** instead of a session.
Only URLs this service minted will serve, and they expire after
`IMAGE_URL_TTL_DAYS` (default 30 — a TTL shorter than the downstream Gemini run
is a silent failure at the last step).

Nothing auto-deletes images: they have to outlive the run that consumes them, and
deleting one whose source URL has expired is unrecoverable.

---

## Output contract

`ready_for_ocr.jsonl`, one object per post:

```jsonc
{
  "post_id": "...", "group_id": "...", "post_link": "...", "author": "...",
  "story_post_id": null, "tile_id": null,
  "images_prepared": 2, "images_failed": 0,
  "images": [{
    "url": "https://<domain>/img/<post_id>/0.jpg?exp=...&sig=...",  // Gemini fetches this
    "idx": 0, "width": 1170, "height": 1461, "bytes": 284113,
    "content_type": "image/jpeg", "sha256": "...",
    "source_url": "https://scontent....fbcdn.net/...",
    "source_expires_at": "2026-08-18T03:06:59+00:00",
    "url_expires_at": "2026-09-16T...", "downloaded_at": "..."
  }],
  "source_key": "...", "source_run_id": "...", "run_id": "...",
  "stage": "han_scan", "schema_version": "han_scan/2.0",
  "prepared_at": "...", "label": "...", "sub_caption": "...", "posted_at": null
}
```

Schema 2.0 **removed** the Han-detection fields (`han_valid`, `han_words_total`,
`valid_pic`, `scan_status`, …) rather than leaving them permanently null. A null
field that can never be filled invites a consumer to read it as `false` and
silently drop every post.

Errors carry an `error_class` and a `retryable` flag, so a retry sweep skips
permanently dead links (`expired_url`, `http_404`) and only re-runs transient ones
(`timeout`, `http_429`, `http_5xx`).

---

## Where things land

Under `DATA_DIR`, mirroring the MinIO layout so files from either path are
interchangeable to the Gemini stage:

| Path | Contents |
|---|---|
| `results/export/ready_for_ocr.jsonl` | The export → feeds Gemini |
| `results/errors/failed.jsonl` | Cumulative failures, for retry sweeps |
| `results/logs/by_run/<run>/…` | Per-run `result.json`, `upserts.jsonl`, `errors.jsonl` |
| `state/processed_ids.jsonl` | The checkpoint |
| `uploads/<id>/valid_post.jsonl` | Uploaded exports |
| `images/` | Downloaded images, served to Gemini |

Under MinIO (when configured) the same tree lives at
`<bucket>/<group_prefix>/han_scan/`. The `han_scan` name is kept as an identifier
the crawl team may already reference — renaming it would orphan existing objects.

---

## Running it

```bash
cp .env.example .env      # then fill it in — the app refuses to start without AUTH_SECRET
```

```bash
docker compose up --build -d && docker compose logs -f scanner
```

Six values need filling: `AUTH_SECRET`, `APP_USERNAME`, `APP_PASSWORD_HASH`,
`COOKIE_SECURE`, `PUBLIC_BASE_URL`, `IMAGE_SIGNING_SECRET`. Generate secrets with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

The app binds `127.0.0.1:8000`; put a reverse proxy in front for the public
domain. `--workers 1` is **required** — batch state lives in the process, and a
second uvicorn worker would fork it and corrupt progress tracking.

### Headless

The core has no web dependency, so it also runs without the app:

```bash
docker compose run --rm scanner python -m app.cli --check
```

```bash
docker compose run --rm scanner python -m app.cli --preflight-only
```

```bash
docker compose run --rm scanner python -m app.cli --limit 500
```

Add `--minio` to read the crawler's by_run logs instead of an upload. `--check`
reports configuration and does **not** fail when MinIO is absent — that's normal.

---

## Architecture

```
app/
  core/     the pipeline — NO web imports, ever
            models  parser  source  sink  storage  downloader  batch
            jobstore  scheduler  signing  imagestore  gallery  health
  api/      the HTTP adapter — FastAPI lives only here
  cli.py    headless runner (proves core is genuinely decoupled)
```

`core` talks to two protocols, so either side can be swapped without touching
pipeline logic:

```python
class RecordSource(Protocol):
    def iter_pending(self, limit: int) -> PendingBatch: ...
    def mark_done(self, post_ids: list[str], run_id: str) -> None: ...

class ResultSink(Protocol):
    def write_results(self, records: list[PreparedPost], run_id: str) -> None: ...
    def write_errors(self, errors: list[PrepError], run_id: str) -> None: ...
```

Shipped: `FileRecordSource` / `MinioRecordSource`, and `FileResultSink` /
`MinioResultSink` / `TeeResultSink` (local plus mirror).

**Unknown record shapes:** `core/parser.py` ships a `RecordParser` protocol with a
`CustomRecordParser` stub. Implement `parse()` there for a schema the default does
not handle; nothing else changes.

---

## Tests

The suite needs none of the runtime stack — no MinIO, no FastAPI — so it runs
anywhere in seconds:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

What it deliberately does not cover is anything requiring real I/O. Verify those
on the VPS with `app.cli --check`, `--preflight-only`, and a small `--limit` run,
and by fetching one exported `url` **from outside your network, unauthenticated** —
that last one is what Gemini will do, and the only test that proves the deliverable
works.
