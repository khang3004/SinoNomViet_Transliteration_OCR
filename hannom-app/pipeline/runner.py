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

from pipeline import correct, layouts, ocr, translate
from pipeline.config import Config
from pipeline.page_context import PageContext
from pipeline.schema import Record, write_jsonl

logger = logging.getLogger("hannom.runner")

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def process_file(
    input_path: str, output_path: str, config: Config, source_doc: str = "", engine=None
) -> list[Record]:
    """Process one uploaded file and write its records to ``output_path``.

    Returns the records (also written to disk). ``engine`` may be a pre-built,
    warm OCR engine (the worker keeps one loaded); if None it is built here.
    """
    config.validate()  # fail fast if a selected api backend lacks its key
    if engine is None:
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

    _apply_correction(records, config)  # Bug 3: optional Han OCR proofreading
    _apply_translation(records, config)

    write_jsonl(records, output_path)
    logger.info("Wrote %d record(s) → %s", len(records), output_path)
    return records


def reocr_region(page_image_path: str, bbox, config: Config, engine=None) -> dict:
    """Re-OCR one region of a rendered page image (Bug/feature: box re-OCR).

    Crops ``page_image_path`` to ``bbox`` ([x0,y0,x1,y1] in page-pixel coords)
    and runs the OCR engine on the crop. Returns ``{text, conf}`` with the text
    assembled in reading order (top→bottom, left→right). Used by the worker to
    serve interactive re-OCR of user-edited boxes.
    """
    from PIL import Image

    if engine is None:
        engine = ocr.get_engine(config.ocr_backend)
    x0, y0, x1, y1 = (int(round(v)) for v in bbox)
    img = Image.open(page_image_path)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    if x1 <= x0 or y1 <= y0:
        return {"text": "", "conf": 0.0}
    crop = img.crop((x0, y0, x1, y1))
    from pipeline.imageproc import clean_for_ocr

    crop = clean_for_ocr(crop)  # drop watermark before OCR
    work_dir = getattr(config, "work_dir", None) or "."
    os.makedirs(work_dir, exist_ok=True)
    crop_path = os.path.join(work_dir, "reocr_crop.png")
    crop.save(crop_path)
    from pipeline.layouts.base import cjk_ratio

    dets = engine.ocr(crop_path) or []
    # Keep CJK-majority detections (this targets the Hán column), dropping any
    # Latin label-bleed grazed at the box edges. Read top→bottom, left→right.
    dets = [d for d in dets if cjk_ratio(d.get("text", "")) >= 0.34]
    dets.sort(key=lambda d: ((d["bbox"][1] + d["bbox"][3]) / 2.0, d["bbox"][0]))
    text = "".join(d.get("text", "") for d in dets)
    conf = sum(float(d.get("conf", 0.0)) for d in dets) / len(dets) if dets else 0.0
    return {"text": text, "conf": round(conf, 4)}


def _apply_correction(records: list[Record], config: Config) -> None:
    """Bug 3: correct the Han column via the selected CORRECT_BACKEND.

    Default ``skip`` is a no-op (``han`` == ``han_raw``). Otherwise the corrected
    text goes to ``han`` while ``han_raw`` keeps the original OCR so reviewers can
    see what changed.
    """
    if config.correct_backend == "skip":
        return
    targets = [r for r in records if r.han.strip()]
    if not targets:
        return
    corrector = correct.get_corrector(config)
    logger.info(
        "Correcting Han for %d record(s) with backend %r.",
        len(targets),
        config.correct_backend,
    )
    for rec in targets:
        if not rec.han_raw:
            rec.han_raw = rec.han
        fixed = corrector.correct(rec.han_raw, context=rec.meaning)
        if fixed and fixed != rec.han:
            rec.han = fixed
            rec.han_chars = list(fixed)


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
    failed: list[int] = []
    for page_index in range(n):
        page_no = page_index + 1
        # Isolate each page: a single bad page (unrenderable scan, a non-Mục-lục
        # divider/blank page that matches no layout, etc.) must NOT sink the whole
        # multi-hundred-page job. Log it and carry on.
        try:
            # Rasterise the page ONCE: saved for the UI and reused for Han OCR.
            image, _scale = render_page(pdf_path, page_index, config.pdf_dpi)
            img_name = f"{stem}_p{page_no:04d}.png"
            image.save(os.path.join(pages_dir, img_name))

            ctx = PageContext(
                source_doc=doc,
                page=page_no,
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
        except Exception as exc:  # noqa: BLE001 - one bad page shouldn't fail the job
            failed.append(page_no)
            logger.warning(
                "Skipping page %d of %s (%s: %s).",
                page_no, os.path.basename(pdf_path), type(exc).__name__, exc,
            )
    if failed:
        logger.warning(
            "%s: extracted %d page(s); skipped %d unprocessable page(s): %s",
            os.path.basename(pdf_path), n - len(failed), len(failed), failed,
        )
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
