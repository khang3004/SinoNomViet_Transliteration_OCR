"""PageContext — the unit of work passed to layout handlers.

A handler's ``detect(page_ctx)`` and ``extract(page_ctx)`` receive one of these.
It bundles everything a handler may need: the page image, the (optional) source
PDF + page index, the OCR engine to use for image regions, the parsed config,
and — crucially for dev/testing — an optional set of MOCK text spans so the
two_column path can be exercised without a real text-layer PDF (AGENTS.md §1).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pipeline.pdf_text import TextSpan, extract_spans, has_text_layer, render_page

if TYPE_CHECKING:  # avoid importing the OCR registry at module import time
    from pipeline.ocr.base import OCREngine

logger = logging.getLogger("hannom.page_context")


@dataclass
class PageContext:
    """Everything a layout handler needs to process a single page."""

    source_doc: str
    page: int
    image_path: str = ""
    pdf_path: str | None = None
    pdf_page_index: int = 0
    ocr_engine: "OCREngine | None" = None
    config: object | None = None
    # Page width in pixels, when known (e.g. from the rendered page raster).
    # Lets two_column confirm the Vietnamese column sits on the RIGHT half.
    page_width: float | None = None
    # DPI at which the PDF page is rasterised for Han-side OCR. Text spans are
    # scaled by render_dpi/72 so the Vietnamese text layer and the Han OCR crop
    # share ONE coordinate space (pixels at render_dpi). 72 ⇒ scale 1 (no PDF).
    render_dpi: int = 72

    # MOCK injection point (AGENTS.md §1 / §11.4): when set, these spans are used
    # instead of reading a real PDF text layer. Used by the two_column dry-run.
    mock_text_spans: list[TextSpan] | None = None
    # MOCK Han OCR result injection — list of {text, bbox, conf}. When set, the
    # handler uses it instead of calling the real OCR engine.
    mock_han_ocr: list[dict] | None = field(default=None)

    # ------------------------------------------------------------------
    @property
    def _scale(self) -> float:
        return self.render_dpi / 72.0

    def has_text_layer(self) -> bool:
        """True if mock spans were injected, or the real PDF has a text layer."""
        if self.mock_text_spans is not None:
            return len(self.mock_text_spans) > 0
        if self.pdf_path:
            return has_text_layer(self.pdf_path, self.pdf_page_index)
        return False

    def text_spans(self) -> list[TextSpan]:
        """Return text-layer spans in raster-pixel space (scaled by render_dpi/72).

        Mock spans are returned as-is (the mock fixture already lives in a single
        self-consistent pixel space alongside ``mock_han_ocr``).
        """
        if self.mock_text_spans is not None:
            return list(self.mock_text_spans)
        if self.pdf_path:
            return extract_spans(self.pdf_path, self.pdf_page_index, scale=self._scale)
        return []

    def han_side_ocr(self, split_x: float) -> list[dict]:
        """Return Han OCR detections (left of ``split_x``) in page-pixel space.

        Three sources, in priority:
          1. ``mock_han_ocr`` (dev/testing) — returned as-is.
          2. a real PDF — render the page at ``render_dpi``, crop the left column
             ``[0, split_x]``, OCR that crop. Crop origin is (0,0) so detection
             coords are already in full-page pixel space and align with the
             (scaled) text spans for y-overlap pairing.
          3. a plain image — OCR the whole image; the handler filters by side.
        """
        if self.mock_han_ocr is not None:
            return list(self.mock_han_ocr)
        if self.ocr_engine is None:
            logger.warning("han_side_ocr: no OCR engine; Han side will be empty.")
            return []
        if self.pdf_path:
            return self._ocr_pdf_left_column(split_x)
        # Plain image (e.g. sample page): OCR the whole file.
        return self.ocr_engine.ocr(self.image_path)

    def _ocr_pdf_left_column(self, split_x: float) -> list[dict]:
        """Render the PDF page, crop the Han (left) column, OCR it."""
        image, _scale = render_page(self.pdf_path, self.pdf_page_index, self.render_dpi)
        crop_x = max(int(split_x), 1)
        crop = image.crop((0, 0, min(crop_x, image.width), image.height))
        # Save the crop to the work dir and OCR by path (works for every engine).
        work_dir = getattr(self.config, "work_dir", None) or "."
        os.makedirs(work_dir, exist_ok=True)
        crop_path = os.path.join(work_dir, f"han_crop_p{self.page:04d}.png")
        crop.save(crop_path)
        # Crop origin is (0,0) ⇒ returned bboxes are already full-page coords.
        return self.ocr_engine.ocr(crop_path)
