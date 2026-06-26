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
        records = _process_pdf(input_path, doc, engine, config)
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


def _process_pdf(pdf_path: str, doc: str, engine, config: Config) -> list[Record]:
    # TODO(real-pdf): iterate every page. Per page, render the Han-side crop at
    # config.pdf_dpi for OCR and read the Vietnamese text layer via pdf_text.
    # Validate against a real Châu bản text-layer PDF when one is added.
    import pdfplumber  # lazy, worker-only

    all_records: list[Record] = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
    for page_index in range(n):
        ctx = PageContext(
            source_doc=doc,
            page=page_index + 1,
            image_path="",
            pdf_path=pdf_path,
            pdf_page_index=page_index,
            ocr_engine=engine,
            config=config,
        )
        handler = layouts.route(ctx)
        all_records.extend(handler.extract(ctx))
    return all_records


def _infer_source_doc(path: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    lower = name.lower()
    if "chau" in lower or "châu" in lower:
        return "ChauBan"
    if "uctrai" in lower or "uc_trai" in lower:
        return "UcTraiTap"
    return name or "Unknown"
