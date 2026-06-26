"""PaddleOCR engine (AGENTS.md §3.1, §2).

PaddleOCR on GPU fits the 6 GB GTX 2060 comfortably; this is the DEFAULT
``OCR_BACKEND``. Paddle is imported lazily inside ``__init__`` so that merely
importing the registry (e.g. in the app service or in dry-runs) never requires
Paddle to be installed.
"""

from __future__ import annotations

import logging

from pipeline.ocr import register
from pipeline.ocr.base import Detection, ImageInput

logger = logging.getLogger("hannom.ocr.paddle")


class PaddleEngine:
    """Thin adapter over PaddleOCR returning the common Detection shape."""

    name = "paddle"

    def __init__(self, lang: str = "ch", use_gpu: bool = True) -> None:
        # Lazy import: only the worker (GPU host / container) needs paddleocr.
        from paddleocr import PaddleOCR  # type: ignore

        logger.info("Initialising PaddleOCR (lang=%s, use_gpu=%s)", lang, use_gpu)
        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu)

    def ocr(self, image: ImageInput) -> list[Detection]:
        raw = self._ocr.ocr(image, cls=True)
        detections: list[Detection] = []
        # PaddleOCR returns [[ (poly, (text, conf)), ... ]] (one list per image).
        pages = raw or []
        for page in pages:
            for poly, (text, conf) in page or []:
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                detections.append(
                    Detection(
                        text=text,
                        bbox=[min(xs), min(ys), max(xs), max(ys)],
                        conf=float(conf),
                    )
                )
        return detections


register("paddle", PaddleEngine)
