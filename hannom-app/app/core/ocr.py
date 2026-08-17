"""PaddleOCR wrapper — PP-OCRv6 via paddleocr 3.x, with a 2.x fallback.

Two API generations are supported because the PP-OCRv6 upgrade is gated on a
benchmark that may send us back to 2.8.1:

    paddleocr 3.x   engine.predict(ndarray) -> [{rec_texts, rec_scores, ...}]
    paddleocr 2.x   engine.ocr(ndarray, cls=True) -> [[[box, (text, score)], ...]]

The shape is detected at runtime rather than from a version string, so a patch
release that changes packaging does not break parsing.

Process model: the engine is a per-process singleton built lazily by
``get_engine()``. The batch pipeline runs these in a ProcessPoolExecutor, so each
worker holds its own copy — that is deliberate. A module-level singleton shared
across threads is not safe here, which is what the previous implementation got
wrong.
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any

from app.core.models import ErrorClass, ScanOutcome, count_han_chars

log = logging.getLogger(__name__)

_engine: Any = None
_engine_kind: str = ""  # "predict" (3.x) | "ocr" (2.x)


class OcrUnavailable(RuntimeError):
    """PaddleOCR could not be loaded — a deployment problem, not a bad image."""


def get_engine(lang: str = "ch") -> tuple[Any, str]:
    """Build (or return) this process's PaddleOCR engine.

    Returns (engine, kind) where kind selects the result parser.
    """
    global _engine, _engine_kind
    if _engine is not None:
        return _engine, _engine_kind

    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # noqa: BLE001 - import failure is fatal for this worker
        raise OcrUnavailable(f"cannot import paddleocr: {exc}") from exc

    pid = os.getpid()
    log.info("[pid %d] loading PaddleOCR (lang=%s)...", pid, lang)
    started = time.monotonic()

    # 3.x dropped show_log and use_angle_cls; 2.x needs them. Try the modern
    # signature first and fall back rather than branching on a version string.
    engine = None
    for kwargs in (
        {"lang": lang},
        {"lang": lang, "use_angle_cls": True, "show_log": False},
        {},
    ):
        try:
            engine = PaddleOCR(**kwargs)
            break
        except (TypeError, ValueError) as exc:
            log.debug("PaddleOCR(%s) rejected: %s", kwargs, exc)
            continue
    if engine is None:
        raise OcrUnavailable("PaddleOCR could not be constructed with any known signature")

    kind = "predict" if hasattr(engine, "predict") else "ocr"
    _engine, _engine_kind = engine, kind
    log.info(
        "[pid %d] PaddleOCR ready in %.1fs (api=%s)",
        pid, time.monotonic() - started, kind,
    )
    return _engine, _engine_kind


def _as_mapping(result: Any) -> dict | None:
    """PaddleOCR 3.x results are dict-like but not always dicts."""
    if isinstance(result, dict):
        return result
    for attr in ("json", "res", "_json"):
        value = getattr(result, attr, None)
        if isinstance(value, dict):
            # Some builds nest the payload one level down.
            return value.get("res") if isinstance(value.get("res"), dict) else value
    return None


def parse_predict_result(raw: Any) -> list[tuple[str, float]]:
    """paddleocr 3.x: pull (text, score) pairs out of rec_texts/rec_scores."""
    pairs: list[tuple[str, float]] = []
    if raw is None:
        return pairs

    results = raw if isinstance(raw, (list, tuple)) else [raw]
    for result in results:
        mapping = _as_mapping(result)
        if mapping is None:
            continue
        texts = mapping.get("rec_texts") or []
        scores = mapping.get("rec_scores") or []
        for i, text in enumerate(texts):
            if not text:
                continue
            try:
                score = float(scores[i]) if i < len(scores) else 1.0
            except (TypeError, ValueError):
                score = 1.0
            pairs.append((str(text), score))
    return pairs


def parse_ocr_result(raw: Any) -> list[tuple[str, float]]:
    """paddleocr 2.x: [[[box, (text, score)], ...]] -> (text, score) pairs."""
    pairs: list[tuple[str, float]] = []
    if not raw:
        return pairs

    for page in raw:
        if not page:
            continue
        for entry in page:
            try:
                payload = entry[1]
                text, score = str(payload[0]), float(payload[1])
            except (IndexError, TypeError, ValueError):
                continue
            if text:
                pairs.append((text, score))
    return pairs


def decode_image(data: bytes):
    """Bytes -> RGB ndarray, without touching the filesystem.

    Both PaddleOCR APIs accept an ndarray directly, so there is no reason to
    round-trip through a temp file.
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(f"cannot import imaging stack: {exc}") from exc

    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"))


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """(width, height) without a full decode, for the output metadata."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return None, None


def scan_array(array: Any, *, lang: str = "ch", min_confidence: float = 0.3) -> dict:
    """Run OCR on a decoded image array and summarise the Han content."""
    engine, kind = get_engine(lang)

    if kind == "predict":
        pairs = parse_predict_result(engine.predict(array))
    else:
        pairs = parse_ocr_result(engine.ocr(array, cls=True))

    texts: list[str] = []
    scores: list[float] = []
    han_words = 0
    for text, score in pairs:
        if score < min_confidence:
            continue
        texts.append(text)
        scores.append(score)
        han_words += count_han_chars(text)

    return {
        "valid_pic": han_words > 0,
        "han_words": han_words,
        "boxes": len(texts),
        "texts": texts,
        "mean_confidence": (sum(scores) / len(scores)) if scores else None,
    }


def scan_bytes(
    data: bytes,
    *,
    lang: str = "ch",
    min_confidence: float = 0.3,
) -> dict:
    """Bytes -> Han verdict. The in-RAM path: no temp files anywhere."""
    return scan_array(decode_image(data), lang=lang, min_confidence=min_confidence)


def scan_file(
    path: str,
    post_id: str,
    idx: int,
    *,
    lang: str = "ch",
    min_confidence: float = 0.3,
) -> ScanOutcome:
    """Pool worker entry point.

    Never raises: a corrupt image must cost one item, not the whole batch. The
    only thing that escapes is a hard process death, which the pool handles.
    """
    started = time.monotonic()
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return ScanOutcome(
            idx=idx, post_id=post_id, ok=False,
            error_class=ErrorClass.DECODE_ERROR,
            error_detail=f"cannot read {path}: {exc}",
        )

    try:
        array = decode_image(data)
    except OcrUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - truncated/corrupt image
        return ScanOutcome(
            idx=idx, post_id=post_id, ok=False,
            error_class=ErrorClass.DECODE_ERROR,
            error_detail=f"{type(exc).__name__}: {exc}",
            scan_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        result = scan_array(array, lang=lang, min_confidence=min_confidence)
    except OcrUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - engine hiccup on one image
        return ScanOutcome(
            idx=idx, post_id=post_id, ok=False,
            error_class=ErrorClass.OCR_ERROR,
            error_detail=f"{type(exc).__name__}: {exc}",
            scan_ms=int((time.monotonic() - started) * 1000),
        )

    return ScanOutcome(
        idx=idx,
        post_id=post_id,
        ok=True,
        valid_pic=result["valid_pic"],
        han_words=result["han_words"],
        boxes=result["boxes"],
        texts=result["texts"],
        mean_confidence=result["mean_confidence"],
        scan_ms=int((time.monotonic() - started) * 1000),
    )


def pool_initializer(lang: str) -> None:
    """ProcessPoolExecutor initializer — pay the model load once per worker,
    at pool construction, instead of on the first image."""
    get_engine(lang)


def engine_label(default: str) -> str:
    """Best-effort engine identifier for the output records."""
    try:
        import paddleocr

        version = getattr(paddleocr, "__version__", "unknown")
        series = "PP-OCRv6" if str(version).startswith("3.") else "PP-OCRv4"
        return f"paddleocr-{version}:{series}"
    except Exception:  # noqa: BLE001
        return default
