"""PageContext — the unit of work passed to layout handlers.

A handler's ``detect(page_ctx)`` and ``extract(page_ctx)`` receive one of these.
It bundles everything a handler may need: the page image, the (optional) source
PDF + page index, the OCR engine to use for image regions, the parsed config,
and — crucially for dev/testing — an optional set of MOCK text spans so the
two_column path can be exercised without a real text-layer PDF (AGENTS.md §1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pipeline.pdf_text import TextSpan, extract_spans, has_text_layer

if TYPE_CHECKING:  # avoid importing the OCR registry at module import time
    from pipeline.ocr.base import OCREngine


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

    # MOCK injection point (AGENTS.md §1 / §11.4): when set, these spans are used
    # instead of reading a real PDF text layer. Used by the two_column dry-run.
    mock_text_spans: list[TextSpan] | None = None
    # MOCK Han OCR result injection — list of {text, bbox, conf}. When set, the
    # handler uses it instead of calling the real OCR engine.
    mock_han_ocr: list[dict] | None = field(default=None)

    # ------------------------------------------------------------------
    def has_text_layer(self) -> bool:
        """True if mock spans were injected, or the real PDF has a text layer."""
        if self.mock_text_spans is not None:
            return len(self.mock_text_spans) > 0
        if self.pdf_path:
            return has_text_layer(self.pdf_path, self.pdf_page_index)
        return False

    def text_spans(self) -> list[TextSpan]:
        """Return text-layer spans — mock if injected, else from the real PDF."""
        if self.mock_text_spans is not None:
            return list(self.mock_text_spans)
        if self.pdf_path:
            return extract_spans(self.pdf_path, self.pdf_page_index)
        return []
