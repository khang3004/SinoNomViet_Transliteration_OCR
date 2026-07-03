"""LLM tasks for the corpus: Han correction and Han→Vietnamese translation.

Provider-agnostic prompt builders on top of ``pipeline.llm.complete``. The API
key is supplied by the caller (the user's own key), per request.
"""

from __future__ import annotations

import logging

from pipeline import llm

logger = logging.getLogger("hannom.llm.tasks")

_CORRECT_SYSTEM = (
    "You are a classical Hán-Nôm proofreader. The input is OCR output of classical "
    "Han text from Nguyễn-dynasty Châu bản (Vietnamese royal records). Correct "
    "obvious OCR character errors (shape-similar mis-reads). When a Vietnamese "
    "translation is provided, use it to choose the correct characters. Return ONLY "
    "the corrected Han text — no explanation, no punctuation changes, no "
    "romanization. Keep the same number of characters where possible."
)

_TRANSLATE_SYSTEM = (
    "You translate classical Sino-Vietnamese (Hán-Nôm) / classical Chinese from "
    "Nguyễn-dynasty Châu bản into modern Vietnamese (Quốc Ngữ). Return ONLY the "
    "Vietnamese meaning — no transliteration, no quotes, no Han characters."
)

_VISION_SYSTEM = (
    "You are a classical Hán-Nôm expert reading a CROPPED image from a "
    "Nguyễn-dynasty Châu bản (Vietnamese royal record) 'Mục lục' page. Read the "
    "Han/Nôm characters visible in the image, in natural reading order. An OCR "
    "guess may be provided (it may be empty or wrong) — trust the IMAGE over the "
    "guess. Return ONLY the Han characters you see — no romanization, no "
    "translation, no explanation, no punctuation you don't see in the image."
)


def correct_han(provider: str, api_key: str, han: str, meaning: str = "", model: str | None = None) -> str:
    """Proofread ``han`` (optionally using its Vietnamese ``meaning`` as context)."""
    han = (han or "").strip()
    if not han:
        return han
    prompt = f"OCR Han:\n{han}\n"
    if meaning.strip():
        prompt += f"\nVietnamese translation (use to disambiguate):\n{meaning.strip()}\n"
    prompt += "\nCorrected Han:"
    try:
        out = llm.complete(provider, prompt, api_key=api_key, model=model, system=_CORRECT_SYSTEM)
        return out or han
    except Exception:  # noqa: BLE001 - surface a clean failure to the caller
        logger.exception("LLM correction failed (provider=%s).", provider)
        raise


def translate_han(provider: str, api_key: str, han: str, model: str | None = None) -> str:
    """Translate ``han`` into modern Vietnamese."""
    han = (han or "").strip()
    if not han:
        return ""
    prompt = f"Han text:\n{han}\n\nModern Vietnamese meaning:"
    out = llm.complete(provider, prompt, api_key=api_key, model=model, system=_TRANSLATE_SYSTEM)
    return out


def vision_read_han(
    provider: str, api_key: str, image_bytes: bytes, ocr_text: str = "", model: str | None = None
) -> str:
    """Read Han characters directly from a cropped page image (PNG bytes).

    Used when PaddleOCR is wrong or missed a region: the reviewer draws a box and
    we send the crop + the current OCR text (which may be blank) to the model.
    """
    prompt = "Read the Han characters in this cropped image."
    if (ocr_text or "").strip():
        prompt += f"\nCurrent OCR guess (may be wrong or blank): {ocr_text.strip()}"
    prompt += "\nHan characters:"
    try:
        return llm.complete_vision(
            provider, prompt, image_bytes, api_key=api_key, model=model, system=_VISION_SYSTEM
        )
    except Exception:  # noqa: BLE001 - surface a clean failure to the caller
        logger.exception("LLM vision read failed (provider=%s).", provider)
        raise
