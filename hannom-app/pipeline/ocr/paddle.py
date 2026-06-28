"""PaddleOCR engine (AGENTS.md §3.1, §2).

PaddleOCR on GPU fits the 6 GB GTX 2060 comfortably; this is the DEFAULT
``OCR_BACKEND``. It also runs on **CPU** (slower) for GPU-less hosts — set
``OCR_USE_GPU=0`` (e.g. when sharing the app from a laptop). Paddle is imported
lazily inside ``__init__`` so that merely importing the registry (e.g. in the
app service or in dry-runs) never requires Paddle to be installed.

Env knobs:
  ``OCR_USE_GPU`` — "1" (default) GPU, "0" CPU.
  ``OCR_LANG``    — PaddleOCR language model (default "chinese_cht" for Traditional Chinese).
"""

from __future__ import annotations

import logging
import os

from pipeline.ocr import register
from pipeline.ocr.base import Detection, ImageInput

logger = logging.getLogger("hannom.ocr.paddle")


def _env_use_gpu() -> bool:
    return os.environ.get("OCR_USE_GPU", "1").strip().lower() not in {"0", "false", "no"}


class PaddleEngine:
    """Thin adapter over PaddleOCR returning the common Detection shape."""

    name = "paddle"

    def __init__(self, lang: str | None = None, use_gpu: bool | None = None, **kwargs) -> None:
        # Lazy import: only the worker (GPU/CPU host or container) needs paddleocr.
        from paddleocr import PaddleOCR  # type: ignore

        # CRITICAL FIX 1: Change default to 'chinese_cht' (Traditional) instead of 'ch' (Simplified)
        lang = lang or os.environ.get("OCR_LANG", "chinese_cht")
        use_gpu = _env_use_gpu() if use_gpu is None else use_gpu
        
        # CRITICAL FIX 2: Default configuration optimized for high-res/historical documents
        default_opts = {
            "use_angle_cls": True,
            "lang": lang,
            "use_gpu": use_gpu,
            # Increase the image limit size (default 960). High res is needed for complex Traditional strokes.
            "det_limit_side_len": kwargs.pop("det_limit_side_len", 2048),
            # Slightly lower the box threshold to catch faded or broken text lines (default 0.6)
            "det_db_box_thresh": kwargs.pop("det_db_box_thresh", 0.5),
            # Slightly expand the bounding box to avoid clipping character strokes (default 1.5)
            "det_db_unclip_ratio": kwargs.pop("det_db_unclip_ratio", 1.6),
        }
        
        # Allow overriding with passed arguments
        default_opts.update(kwargs)

        logger.info("Initialising PaddleOCR (opts=%s)", default_opts)
        self._ocr = PaddleOCR(**default_opts)

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