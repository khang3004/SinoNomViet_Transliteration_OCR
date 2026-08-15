"""Facebook Han Scanner — detects Han/CJK characters in images.

Takes Facebook post JSON (with image URLs), downloads the image,
runs PaddleOCR, and tells you if the image has Han characters or not.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.scanner import scan_from_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Han Scanner", version="1.0.0")


# ---------- Models ----------

class ScanRequest(BaseModel):
    id: str
    label: str | None = None
    images: list[str]
    post_link: str | None = None
    author: str | None = None


class ScanResult(BaseModel):
    id: str
    valid_pic: bool
    han_words: int
    texts: list[str]
    boxes: int
    image_url: str
    author: str | None = None
    post_link: str | None = None


# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
async def ui():
    """Serve the test UI."""
    with open("app/static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/scan", response_model=ScanResult)
async def scan(req: ScanRequest):
    """Scan the first image in the post for Han characters."""
    if not req.images:
        raise HTTPException(400, "No images in the request")

    image_url = req.images[0]
    log.info("Scanning image for post %s from %s", req.id, req.author or "unknown")

    try:
        result = await scan_from_url(image_url)
    except Exception as e:
        log.error("Failed to scan image: %s", e)
        raise HTTPException(500, f"Failed to scan image: {e}")

    return ScanResult(
        id=req.id,
        valid_pic=result["valid_pic"],
        han_words=result["han_words"],
        texts=result["texts"],
        boxes=result["boxes"],
        image_url=image_url,
        author=req.author,
        post_link=req.post_link,
    )


@app.post("/scan/batch", response_model=list[ScanResult])
async def scan_batch(items: list[ScanRequest]):
    """Scan multiple posts at once."""
    results = []
    for req in items:
        if not req.images:
            continue
        image_url = req.images[0]
        log.info("Scanning image for post %s", req.id)
        try:
            result = await scan_from_url(image_url)
            results.append(ScanResult(
                id=req.id,
                valid_pic=result["valid_pic"],
                han_words=result["han_words"],
                texts=result["texts"],
                boxes=result["boxes"],
                image_url=image_url,
                author=req.author,
                post_link=req.post_link,
            ))
        except Exception as e:
            log.error("Failed to scan post %s: %s", req.id, e)
            results.append(ScanResult(
                id=req.id,
                valid_pic=False,
                han_words=0,
                texts=[f"ERROR: {e}"],
                boxes=0,
                image_url=image_url,
                author=req.author,
                post_link=req.post_link,
            ))
    return results
