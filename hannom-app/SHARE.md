# Share the app with a friend (instant tunnel + CPU OCR)

Get a public `https://…` URL your friend can open in a browser and upload images
to. No GPU, no cloud account. The URL is live only while your PC + these
terminals are running.

> **What your friend gets when they upload a two-column IMAGE:** real PaddleOCR
> (CPU) of the page, returned via the `han_only` handler — i.e. the Han columns
> are OCR'd. Because a plain image has **no PDF text layer**, the hybrid
> `two_column` Han↔Vietnamese pairing does NOT run on image uploads (that needs a
> text-layer PDF). To show the full parallel experience, have them click
> **▶ Run two_column demo** on the page. Image OCR also picks up the Vietnamese
> text and any watermark, so `han_only` output on these pages is rough — that's
> expected for image input.

All commands are PowerShell, run from the `hannom-app/` folder.

## 1. One-time install

Install the CPU OCR stack (numpy pin order matters) + the app deps:

```powershell
cd hannom-app
uv pip install numpy==1.26.4
uv pip install -r requirements-worker-cpu.txt
uv pip install --force-reinstall numpy==1.26.4
uv pip install fastapi "uvicorn[standard]" python-multipart
```

Install the tunnel tool (no account needed):

```powershell
winget install --id Cloudflare.cloudflared
```

## 2. Run it (three terminals)

**Terminal 1 — worker (CPU OCR).** First run downloads the PaddleOCR models
(a few hundred MB), so give it a minute:

```powershell
cd hannom-app
$env:DATA_DIR="./data"; $env:OCR_BACKEND="paddle"; $env:OCR_USE_GPU="0"; $env:TRANSLATE_BACKEND="skip"
uv run python -m worker.worker
```

**Terminal 2 — web app** (bind 0.0.0.0 so the tunnel can reach it):

```powershell
cd hannom-app
$env:DATA_DIR="./data"
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Terminal 3 — public tunnel:**

```powershell
cloudflared tunnel --url http://localhost:8000
```

cloudflared prints a line like:

```
https://random-words-1234.trycloudflare.com
```

Send that URL to your friend. They open it, upload a two-column image, watch the
job go `pending → running → done`, then click **view** to see the OCR records (or
**Run two_column demo** for the full parallel example).

## Notes

- **Public link:** anyone with the URL can upload while it's live. Share it only
  with people you trust, and close Terminal 3 to take it offline.
- **Want Vietnamese meaning too?** Set `TRANSLATE_BACKEND=api` and
  `GOOGLE_API_KEY=…` in Terminal 1 to have Gemini fill the `meaning` field for
  `han_only` records (otherwise meaning stays empty for image uploads).
- **Quick UI check without installing Paddle:** use `OCR_BACKEND=mock` in
  Terminal 1 — returns canned data so you can test the upload→job→view flow, but
  it won't actually read the image.
- **ngrok instead of cloudflared:** `ngrok http 8000` also works (needs a free
  ngrok account/token).
