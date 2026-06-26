"""OCR engine protocol + result type (AGENTS.md §3.1).

An engine takes an image path (or an in-memory crop) and returns a flat list of
detections: ``[{text, bbox, conf}, ...]`` where ``bbox`` is ``[x0, y0, x1, y1]``
in top-left pixel space.

Adding a new engine later = drop a file in ``pipeline/ocr/`` and call
``register("name", cls)``. No existing engine is touched.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, Union, runtime_checkable

# A crop can be passed as a path or as an in-memory image (e.g. numpy array /
# PIL Image). Engines that only accept paths should document that.
ImageInput = Union[str, "object"]


class Detection(TypedDict):
    """One OCR detection."""

    text: str
    bbox: list[float]  # [x0, y0, x1, y1]
    conf: float


@runtime_checkable
class OCREngine(Protocol):
    """Protocol all OCR engines implement."""

    name: str

    def ocr(self, image: ImageInput) -> list[Detection]:
        """Run OCR on an image (path or crop) and return detections."""
        ...
