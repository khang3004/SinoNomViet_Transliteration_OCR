"""Pipeline runner — turn an input file into a JSONL of parallel records.

Glue between the worker and the registries: build :class:`PageContext`(s) for an
uploaded file, let the layout router pick a handler, extract records, write JSONL.

Image inputs (the sample case) have no text layer, so ``two_column.detect`` is
false and the router falls back to ``han_only`` (full-OCR). A real text-layer PDF
would route to ``two_column``.
"""

from __future__ import annotations

import logging
import os

from pipeline import layouts, ocr, translate
from pipeline.config import Config
from pipeline.page_context import PageContext
from pipeline.schema import Record, write_jsonl

logger = logging.getLogger("hannom.runner")

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def process_file(input_path: str, output_path: str, config: Config, source_doc: str = "") -> list[Record]:
    """Process one uploaded file and write its records to ``output_path``.

    Returns the records (also written to disk).
    """
    config.validate()  # fail fast if a selected api backend lacks its key
    engine = ocr.get_engine(config.ocr_backend)
    logger.info("Using OCR backend %r.", config.ocr_backend)

    doc = source_doc or _infer_source_doc(input_path)
    ext = os.path.splitext(input_path)[1].lower()

    records: list[Record] = []
    if ext == ".pdf":
        records = _process_pdf(input_path, doc, engine, config, output_path)
    elif ext in _IMAGE_EXTS:
        records = _process_image(input_path, doc, engine, config)
    else:
        raise ValueError(f"Unsupported input type: {ext!r}")

    _apply_translation(records, config)

    write_jsonl(records, output_path)
    logger.info("Wrote %d record(s) → %s", len(records), output_path)
    return records


def _apply_translation(records: list[Record], config: Config) -> None:
    """Fill empty ``meaning`` fields via the selected translation backend.

    Records that ALREADY have a meaning (e.g. two_column's PDF-text-layer
    Vietnamese) are left untouched — that source is higher trust than MT. Only
    records with Han but no meaning are translated. The translator's
    ``source_tag`` (e.g. "gemini") is recorded in ``source_of.meaning``.
    """
    if config.translate_backend == "skip":
        return
    targets = [r for r in records if r.han.strip() and not r.meaning.strip()]
    if not targets:
        return
    translator = translate.get_translator(config)
    logger.info(
        "Translating %d record(s) with backend %r.",
        len(targets),
        config.translate_backend,
    )
    glosses = translator.translate_many([(r.han, "") for r in targets])
    for rec, vi in zip(targets, glosses):
        if vi.strip():
            rec.meaning = vi.strip()
            rec.source_of.meaning = translator.source_tag


def _process_image(image_path: str, doc: str, engine, config: Config) -> list[Record]:
    ctx = PageContext(
        source_doc=doc,
        page=1,
        image_path=image_path,
        ocr_engine=engine,
        config=config,
    )
    handler = layouts.route(ctx)
    return handler.extract(ctx)


def _process_pdf(pdf_path: str, doc: str, engine, config: Config, output_path: str) -> list[Record]:
    """Process every page of a real PDF.

    Per page: the Vietnamese side comes from the text layer (scaled to render_dpi
    pixel space) and the Han side from OCR of the left-column crop rendered at
    config.pdf_dpi — both owned by PageContext so they stay coordinate-consistent.

    TODO(real-pdf): validate against a genuine text-layer Châu bản PDF (needs
    poppler for rendering + the OCR backend). The logic is exercised by
    tests/test_two_column_pdf.py with the render/OCR/text-layer calls monkeypatched.
    """
    import pdfplumber  # lazy, worker-only
    from pipeline.pdf_text import render_page

    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)

    # Save rendered page images so the UI can show the source page next to its
    # extracted records. Named by the output stem so they're unique per job.
    pages_dir = os.path.join(config.output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    stem = _safe_stem(output_path)

    all_records: list[Record] = []
    for page_index in range(n):
        # Rasterise the page ONCE: saved for the UI and reused for Han OCR.
        image, _scale = render_page(pdf_path, page_index, config.pdf_dpi)
        img_name = f"{stem}_p{page_index + 1:04d}.png"
        image.save(os.path.join(pages_dir, img_name))

        ctx = PageContext(
            source_doc=doc,
            page=page_index + 1,
            image_path=img_name,  # filename the UI fetches from /pages/
            pdf_path=pdf_path,
            pdf_page_index=page_index,
            ocr_engine=engine,
            config=config,
            render_dpi=config.pdf_dpi,
            page_width=float(image.width),
            prerendered_image=image,
        )
        handler = layouts.route(ctx)
        all_records.extend(handler.extract(ctx))
    return all_records


def _safe_stem(output_path: str) -> str:
    """ASCII-safe, URL-safe stem derived from the output file name."""
    import re

    base = os.path.splitext(os.path.basename(output_path))[0]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:80] or "page"


def _infer_source_doc(path: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    lower = name.lower()
    if "chau" in lower or "châu" in lower:
        return "ChauBan"
    if "uctrai" in lower or "uc_trai" in lower:
        return "UcTraiTap"
    return name or "Unknown"
