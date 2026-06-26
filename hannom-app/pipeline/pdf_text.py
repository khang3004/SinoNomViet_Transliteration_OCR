"""PDF text-layer extraction (AGENTS.md §4).

The Châu bản Vietnamese side is a REAL selectable PDF text layer. We exploit
this: extract Vietnamese from the text layer (100% accurate, no OCR, and the
watermark is automatically excluded because it is NOT in the text layer).

Library choice: **pdfplumber** (wraps pdfminer.six). Chosen over PyMuPDF because
(a) its ``page.extract_words()`` already returns per-word boxes in top-left
pixel space which is exactly the span shape we need, and (b) it is pure-Python /
MIT-friendly with no system-library surprises in the slim app image. PyMuPDF
would also work; the dependency is pinned in requirements-worker.txt.

IMPORTANT (test-data reality, AGENTS.md §1): the repo has NO real Châu bản PDF —
only sample page IMAGES. A sample image has no text layer, so for all dev/testing
the text spans are MOCKED (see ``PageContext.mock_text_spans``). The real
``extract_spans`` path below is exercised only when a genuine text-layer PDF is
supplied later.

TODO(real-pdf): real two_column extraction needs a text-layer PDF as input.
Validate ``extract_spans`` / ``has_text_layer`` against a real Châu bản PDF when
one is added; current sample is an image and intentionally falls through to the
full-OCR fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSpan:
    """A word/token from the PDF text layer with its bounding box.

    Coordinates are in top-left pixel space: ``(x0, y0)`` top-left corner,
    ``(x1, y1)`` bottom-right corner.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


def has_text_layer(pdf_path: str, page_index: int = 0, min_words: int = 5) -> bool:
    """Return True if ``page_index`` of ``pdf_path`` has a usable text layer.

    Docs without a text layer (e.g. a scanned image-only page) return False so
    the router can fall back to full-OCR handlers.

    Args:
        pdf_path:   Path to the PDF.
        page_index: 0-based page number.
        min_words:  Minimum extracted words to consider the layer "usable".
    """
    try:
        spans = extract_spans(pdf_path, page_index)
    except Exception:  # noqa: BLE001 - any extraction failure ⇒ no usable layer
        return False
    return len(spans) >= min_words


def extract_spans(pdf_path: str, page_index: int = 0) -> list[TextSpan]:
    """Extract text-layer word spans from one PDF page.

    Uses pdfplumber's ``extract_words()``. ``pdfplumber`` is imported lazily so
    the lightweight ``app`` service (which never parses PDFs) need not install it.

    Returns:
        A list of :class:`TextSpan`, one per word, in PDF reading order.

    Raises:
        ImportError: if pdfplumber is not installed (worker-only dependency).
    """
    import pdfplumber  # lazy: worker-only dependency

    spans: list[TextSpan] = []
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        for w in page.extract_words(use_text_flow=True, keep_blank_chars=False):
            spans.append(
                TextSpan(
                    text=w["text"],
                    x0=float(w["x0"]),
                    y0=float(w["top"]),
                    x1=float(w["x1"]),
                    y1=float(w["bottom"]),
                )
            )
    return spans
