"""Kandianguji OCR engine — STUB (AGENTS.md §3.1).

This file exists purely to PROVE the plug-in path: a brand-new OCR engine is
added by dropping one file here and making a single ``register(...)`` call.
The engine registers successfully and is selectable via ``OCR_BACKEND=kandianguji``,
but raises ``NotImplementedError`` when actually used.

TODO(kandianguji): implement the real API/client adapter. Kandianguji
(古籍 OCR) specialises in classical Chinese woodblock prints and would be a good
fit for the Han side; wire its HTTP client here and return the common
Detection shape ``[{text, bbox, conf}]``.
"""

from __future__ import annotations

from pipeline.ocr import register
from pipeline.ocr.base import Detection, ImageInput


class KandiangujiEngine:
    """Interface-only stub proving the registry plug-in path."""

    name = "kandianguji"

    def ocr(self, image: ImageInput) -> list[Detection]:
        raise NotImplementedError(
            "kandianguji OCR engine is a registered STUB and not yet implemented. "
            "It exists to demonstrate that a new engine plugs in via a single "
            "register() call. Choose OCR_BACKEND=paddle (default) or vision."
        )


register("kandianguji", KandiangujiEngine)
