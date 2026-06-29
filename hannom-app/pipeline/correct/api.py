"""``api`` corrector — Gemini proofreading of the Han column (Bug 3).

Gated by ``CORRECT_BACKEND=api``. Reads ``GOOGLE_API_KEY`` (env only, never
logged). The prompt tells the model these are classical Han from Nguyễn-dynasty
Châu bản and to return ONLY corrected Han, same length where possible.
``google-generativeai`` is imported lazily.
"""

from __future__ import annotations

import logging
import os

from pipeline.correct import register

logger = logging.getLogger("hannom.correct.api")

_SYSTEM = (
    "You are a classical Hán-Nôm proofreader. The input is OCR output of "
    "classical Han text from Nguyễn-dynasty Châu bản (Vietnamese royal records). "
    "Correct obvious OCR character errors (shape-similar mis-reads). When a "
    "Vietnamese translation is provided, use it to choose the correct characters. "
    "Return ONLY the corrected Han text — no explanation, no punctuation changes, "
    "no romanization. Keep the same number of characters where possible."
)


class ApiCorrector:
    name = "api"

    def __init__(self, config=None) -> None:
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set; cannot use CORRECT_BACKEND=api."
            )
        model_id = os.environ.get("CORRECT_MODEL", "gemini-2.0-flash").strip()
        import google.generativeai as genai  # lazy

        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model_id, system_instruction=_SYSTEM)
        logger.info("Gemini Han corrector ready (model=%s).", model_id)

    def correct(self, han: str, context: str = "") -> str:
        han = (han or "").strip()
        if not han:
            return han
        prompt = f"OCR Han:\n{han}\n"
        if context and context.strip():
            prompt += f"\nVietnamese translation (use to disambiguate):\n{context.strip()}\n"
        prompt += "\nCorrected Han:"
        try:
            resp = self._model.generate_content(prompt)
            fixed = (resp.text or "").strip()
            return fixed or han
        except Exception:  # noqa: BLE001 - never fail the job on a proofread error
            logger.exception("Gemini Han correction failed; keeping raw OCR.")
            return han


register("api", ApiCorrector)
