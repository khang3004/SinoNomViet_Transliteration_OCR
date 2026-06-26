"""Google Cloud Vision OCR engine (AGENTS.md §3.1).

Selected via ``OCR_BACKEND=vision``. Reads its key from ``GOOGLE_VISION_KEY``
(env only — never hardcoded/logged). The Vision client is imported lazily so the
registry imports cleanly without the dependency.
"""

from __future__ import annotations

import logging
import os

from pipeline.ocr import register
from pipeline.ocr.base import Detection, ImageInput

logger = logging.getLogger("hannom.ocr.vision")


class VisionEngine:
    """Adapter over Google Cloud Vision document text detection."""

    name = "vision"

    def __init__(self) -> None:
        if not os.environ.get("GOOGLE_VISION_KEY", "").strip():
            # Defensive: config.validate() should already have failed fast.
            raise RuntimeError(
                "GOOGLE_VISION_KEY is not set; cannot use OCR_BACKEND=vision."
            )
        from google.cloud import vision  # type: ignore

        logger.info("Initialising Google Vision client.")
        self._client = vision.ImageAnnotatorClient()
        self._vision = vision

    def ocr(self, image: ImageInput) -> list[Detection]:
        if not isinstance(image, str):
            raise TypeError("VisionEngine.ocr expects an image file path.")
        with open(image, "rb") as fh:
            content = fh.read()
        img = self._vision.Image(content=content)
        resp = self._client.document_text_detection(image=img)
        detections: list[Detection] = []
        for ann in resp.text_annotations[1:]:  # [0] is the full-page string
            xs = [v.x for v in ann.bounding_poly.vertices]
            ys = [v.y for v in ann.bounding_poly.vertices]
            detections.append(
                Detection(
                    text=ann.description,
                    bbox=[float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
                    conf=1.0,  # Vision does not expose per-token confidence here
                )
            )
        return detections


register("vision", VisionEngine)
