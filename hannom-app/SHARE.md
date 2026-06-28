# Share the app with a friend (instant tunnel + CPU OCR)

Get a public `https://…` URL your friend can open in a browser and upload images
to. No GPU, no cloud account. The URL is live only while your PC + these
terminals are running.

> **What your friend gets when they upload a two-column IMAGE:** real PaddleOCR
> (CPU) of the page, returned via the `han_only` handler — i.e. the Han columns
> are OCR'd. Because a plain image has **no PDF text layer**, the hybrid
> `two_column` Han↔Vietnamese pairing does NOT run on image uploads (that needs a
> text-layer PDF). The full parallel experience requires uploading a text-layer
> Mục lục PDF. Image OCR also picks up the Vietnamese text and any watermark, so
> `han_only` output on these pages is rough — that's expected for image input.

All commands are PowerShell, run from the `hannom-app/` folder.

## 1. One-time install

> **Why a separate Python 3.11 env?** PaddleOCR/paddlepaddle have NO wheels for
> very new Pythons (this project's `.venv` is 3.14). So the OCR worker runs in a
> dedicated **Python 3.11** venv. Paddle's CPU wheels live on Paddle's own index,
> not PyPI.

Create the worker env and install the OCR stack (numpy pin order matters):

```powershell
cd hannom-app
uv venv --python 3.11 .venv-worker
$PY = ".\.venv-worker\Scripts\python.exe"
uv pip install --python $PY numpy==1.26.4
uv pip install --python $PY --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ --index-strategy unsafe-best-match paddlepaddle==2.6.1 paddleocr==2.8.1 pdfplumber==0.11.4 pdf2image==1.17.0 setuptools
uv pip install --python $PY --force-reinstall numpy==1.26.4
```

Install **poppler** (renders PDF pages for Han-side OCR) — portable, no admin.
Download the latest zip from
<https://github.com/oschwartz10612/poppler-windows/releases>, unzip it, and note
the path to its `Library\bin` folder (used as `POPPLER_PATH` below).

Install the tunnel tool (no account needed):

```powershell
winget install --id Cloudflare.cloudflared   # or download cloudflared.exe portably
```

## 2. Run it (three terminals)

**Terminal 1 — worker (CPU OCR).** First run downloads the PaddleOCR models, so
give it a minute. Set `POPPLER_PATH` to your unzipped poppler `Library\bin`:

```powershell
cd hannom-app
$env:DATA_DIR="./data"; $env:OCR_BACKEND="paddle"; $env:OCR_USE_GPU="0"; $env:OCR_LANG="chinese_cht"; $env:TRANSLATE_BACKEND="skip"
$env:POPPLER_PATH="$PWD\.tools\poppler-24.08.0\Library\bin"   # adjust to your poppler path
.\.venv-worker\Scripts\python.exe -m worker.worker
```

**Terminal 2 — web app** (bind 0.0.0.0 so the tunnel can reach it). The app is
light and runs fine in the main env:

```powershell
cd hannom-app
$env:DATA_DIR="./data"
uv run --with fastapi --with "uvicorn[standard]" --with python-multipart python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Terminal 3 — public tunnel:**

```powershell
cloudflared tunnel --url http://localhost:8000
```

cloudflared prints a line like:

```
https://random-words-1234.trycloudflare.com
```

Send that URL to your friend. They open it, upload a page (or a text-layer Mục
lục PDF), watch the job go `pending → running → done`, then click **view** to see
the records beside the source page image with block overlays.

## Notes

- **Public link:** anyone with the URL can upload while it's live. Share it only
  with people you trust, and close Terminal 3 to take it offline.
- **Want Vietnamese meaning too?** Set `TRANSLATE_BACKEND=api` and
  `GOOGLE_API_KEY=…` in Terminal 1 to have Gemini fill the `meaning` field for
  `han_only` records (otherwise meaning stays empty for image uploads).
- **Want the Hán OCR errors corrected?** Set `CORRECT_BACKEND=api` +
  `GOOGLE_API_KEY=…` (Gemini proofreads classical Hán; best quality). The raw OCR
  is always kept in `han_raw`. `=dict` uses `dicts/` (only as good as those
  dictionaries); `=skip` (default) leaves Hán exactly as OCR'd.
- **ngrok instead of cloudflared:** `ngrok http 8000` also works (needs a free
  ngrok account/token).
