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
