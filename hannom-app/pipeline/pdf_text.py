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


def _require(module: str):
    """Import a worker-only module with an actionable error if it's missing."""
    import importlib

    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:  # pragma: no cover - env-specific
        raise ModuleNotFoundError(
            f"'{module}' is required to process PDFs but is not installed in this "
            "Python environment. Run the WORKER in its dedicated env, e.g. "
            r".\.venv-worker\Scripts\python.exe -m worker.worker  (see SHARE.md). "
            "The project's default venv (Python 3.14) cannot install PaddleOCR/"
            "pdfplumber — that's why the worker has its own Python 3.11 env."
        ) from exc


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


def extract_spans(pdf_path: str, page_index: int = 0, scale: float = 1.0) -> list[TextSpan]:
    """Extract text-layer word spans from one PDF page.

    Uses pdfplumber's ``extract_words()``. ``pdfplumber`` is imported lazily so
    the lightweight ``app`` service (which never parses PDFs) need not install it.

    pdfplumber yields coordinates in PDF points (72 dpi). Pass ``scale`` =
    ``render_dpi / 72`` to convert spans into the SAME pixel space as a page
    raster rendered at ``render_dpi`` (so the Vietnamese text layer and the Han
    OCR crop share one coordinate system for y-overlap pairing).

    Returns:
        A list of :class:`TextSpan`, one per word, in PDF reading order.

    Raises:
        ImportError: if pdfplumber is not installed (worker-only dependency).
    """
    pdfplumber = _require("pdfplumber")  # lazy: worker-only dependency

    spans: list[TextSpan] = []
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        for w in page.extract_words(use_text_flow=True, keep_blank_chars=False):
            spans.append(
                TextSpan(
                    text=w["text"],
                    x0=float(w["x0"]) * scale,
                    y0=float(w["top"]) * scale,
                    x1=float(w["x1"]) * scale,
                    y1=float(w["bottom"]) * scale,
                )
            )
    return spans


def page_size_points(pdf_path: str, page_index: int = 0) -> tuple[float, float]:
    """Return (width, height) of a PDF page in points (72 dpi)."""
    pdfplumber = _require("pdfplumber")  # lazy: worker-only dependency

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        return float(page.width), float(page.height)


def render_page(pdf_path: str, page_index: int = 0, dpi: int = 300):
    """Render one PDF page to a PIL image at ``dpi`` (for Han-side OCR).

    Uses ``pdf2image`` (poppler / pdftoppm — the ``poppler-utils`` system package
    in the worker image, AGENTS.md §10). Pixel coordinates of the returned raster
    relate to PDF points by ``pixel = point * dpi / 72``.

    Returns:
        ``(pil_image, scale)`` where ``scale = dpi / 72``.

    Raises:
        ImportError: if pdf2image/poppler is not installed (worker-only).
    """
    import os

    convert_from_path = _require("pdf2image").convert_from_path  # lazy: worker-only

    # Allow a portable poppler install (no admin) via POPPLER_PATH=<...>/bin.
    poppler_path = os.environ.get("POPPLER_PATH", "").strip() or None
    pages = convert_from_path(
        pdf_path,
        dpi=dpi,
        first_page=page_index + 1,
        last_page=page_index + 1,
        poppler_path=poppler_path,
    )
    if not pages:
        raise ValueError(f"Could not render page {page_index} of {pdf_path!r}")
    return pages[0], dpi / 72.0
