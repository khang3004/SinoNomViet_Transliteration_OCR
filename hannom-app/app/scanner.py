"""Scanner module — downloads image and detects Han/CJK characters using PaddleOCR."""

from __future__ import annotations

import io
import logging
import re
from typing import Any

import httpx
import numpy as np
from paddleocr import PaddleOCR
from PIL import Image

log = logging.getLogger(__name__)

# CJK Unified Ideographs + Extension A + Compatibility Ideographs
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF\U00020000-\U0002A6DF]"
)

# Lazy-loaded singleton
_ocr: PaddleOCR | None = None


def _get_ocr() -> PaddleOCR:
    global _ocr
    if _ocr is None:
        log.info("Loading PaddleOCR model (chinese_cht) ...")
        _ocr = PaddleOCR(use_angle_cls=True, lang="chinese_cht", show_log=False)
        log.info("PaddleOCR model ready.")
    return _ocr


async def download_image(url: str, timeout: float = 30) -> Image.Image:
    """Download image from URL and return as PIL Image."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def count_han_chars(text: str) -> int:
    """Count CJK/Han characters in a string."""
    return len(_CJK_RE.findall(text))


def scan_image(img: Image.Image) -> dict[str, Any]:
    """Run PaddleOCR on a PIL image and return scan result.

    Returns dict with:
        valid_pic  — True if any Han character found
        han_words  — total count of Han characters detected
        texts      — list of detected text strings (for debugging)
        boxes      — number of text boxes detected
    """
    ocr = _get_ocr()
    arr = np.array(img)
    result = ocr.ocr(arr, cls=True)

    texts: list[str] = []
    total_han = 0

    if result:
        for page in result:
            if not page:
                continue
            for box in page:
                text = box[1][0]
                confidence = box[1][1]
                if confidence < 0.3:
                    continue
                texts.append(text)
                total_han += count_han_chars(text)

    return {
        "valid_pic": total_han > 0,
        "han_words": total_han,
        "texts": texts,
        "boxes": len(texts),
    }


async def scan_from_url(image_url: str) -> dict[str, Any]:
    """Download image from URL and scan for Han characters."""
    img = await download_image(image_url)
    return scan_image(img)
