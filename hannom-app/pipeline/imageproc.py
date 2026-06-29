"""Image preprocessing to improve OCR on watermarked / typeset Châu bản scans.

Châu bản pages carry a light diagonal watermark ("LƯU TRỮ VN" + a logo) that
overlaps the Han text and degrades recognition. Binarizing (Otsu) drops the
light watermark/background while keeping the dark glyphs — a big accuracy win on
these clean typeset pages.

The output keeps the SAME pixel size as the input, so OCR detection coordinates
stay in the page's coordinate space (no remapping needed for y-band pairing).
No-op if OpenCV/NumPy are unavailable or ``OCR_PREPROCESS=0``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("hannom.imageproc")


def preprocessing_enabled() -> bool:
    return os.environ.get("OCR_PREPROCESS", "1").strip().lower() not in {"0", "false", "no"}


def clean_for_ocr(pil_img):
    """Return a high-contrast copy of ``pil_img`` with the watermark removed.

    Grayscale → light median denoise → Otsu threshold. Same size as input.
    Falls back to the original image if cv2/numpy aren't installed.
    """
    if not preprocessing_enabled():
        return pil_img
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception:  # noqa: BLE001 - preprocessing is best-effort
        logger.warning("OCR preprocessing unavailable (cv2/numpy missing); skipping.")
        return pil_img

    gray = np.array(pil_img.convert("L"))
    gray = cv2.medianBlur(gray, 3)  # kill speckle without smearing strokes
    # Otsu picks the threshold between dark text and the lighter watermark/paper:
    # text → black, watermark + background → white.
    _, binar = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(binar).convert("RGB")
