"""Mock OCR engine — for local testing WITHOUT a GPU / PaddleOCR (AGENTS.md §1, §11).

This is NOT one of the production engines from the spec table; it is a developer
convenience so the full app + worker stack can run end-to-end locally on the
sample image with no GPU. Selected via ``OCR_BACKEND=mock``.

It returns a small deterministic set of Han detections (including a watermark-like
non-CJK token) so you can watch the two_column watermark filter and han↔meaning
pairing work without calling Paddle.
"""

from __future__ import annotations

from pipeline.ocr import register
from pipeline.ocr.base import Detection, ImageInput


class MockEngine:
    """Returns canned Han detections for local, GPU-free testing."""

    name = "mock"

    def ocr(self, image: ImageInput) -> list[Detection]:  # noqa: ARG002
        # A short vertical Han column + one watermark token to be filtered.
        return [
            Detection(text="平", bbox=[40, 40, 80, 80], conf=0.97),
            Detection(text="定", bbox=[40, 90, 80, 130], conf=0.96),
            Detection(text="營", bbox=[40, 140, 80, 180], conf=0.95),
            Detection(text="公", bbox=[40, 190, 80, 230], conf=0.94),
            Detection(text="堂", bbox=[40, 240, 80, 280], conf=0.93),
            # Watermark bleed: non-CJK, low confidence ⇒ dropped by post-filter.
            Detection(text="LƯU TRỮ VN", bbox=[55, 300, 200, 340], conf=0.18),
        ]


register("mock", MockEngine)
