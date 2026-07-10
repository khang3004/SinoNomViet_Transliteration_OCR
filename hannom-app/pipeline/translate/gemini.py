"""Gemini translation backend — ``TRANSLATE_BACKEND=api`` (AGENTS.md §6, §7).

Default translation path: ``gemini-flash-latest`` (auto-tracks the newest Flash).
The model id is configurable via ``TRANSLATE_MODEL``. The API key is read from ``GOOGLE_API_KEY``
in the environment — never hardcoded, never logged. ``google-generativeai`` is
imported lazily so importing the registry never requires the dependency.
"""

from __future__ import annotations

import logging
import os

from pipeline.translate import register

logger = logging.getLogger("hannom.translate.gemini")

_SYSTEM_INSTRUCTION = (
    "You are an expert translator of classical Sino-Vietnamese (Hán-Nôm) and "
    "classical Chinese into modern Vietnamese (Quốc Ngữ). Given a Han text "
    "fragment from a Nguyễn-dynasty document, return ONLY its faithful modern "
    "Vietnamese meaning — no transliteration, no quotes, no explanations, no "
    "Han characters. If the input is not meaningful text, return an empty string."
)


class GeminiTranslator:
    """Adapter over the Gemini API for Han → Vietnamese meaning."""

    name = "api"
    source_tag = "gemini"

    def __init__(self, config=None) -> None:
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            # Defensive: config.validate() should already have failed fast.
            raise RuntimeError(
                "GOOGLE_API_KEY is not set; cannot use TRANSLATE_BACKEND=api."
            )
        self._model_id = os.environ.get("TRANSLATE_MODEL", "gemini-flash-latest").strip()
        import google.generativeai as genai  # lazy, worker-only

        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            self._model_id, system_instruction=_SYSTEM_INSTRUCTION
        )
        logger.info("Gemini translator ready (model=%s).", self._model_id)

    def translate(self, han: str, context: str = "") -> str:
        han = (han or "").strip()
        if not han:
            return ""
        prompt = f"Han text:\n{han}\n"
        if context:
            prompt += f"\nContext hint (Vietnamese): {context}\n"
        prompt += "\nModern Vietnamese meaning:"
        try:
            resp = self._model.generate_content(prompt)
            return (resp.text or "").strip()
        except Exception:  # noqa: BLE001 - a failed call must not crash the job
            logger.exception("Gemini translate failed for a fragment; leaving empty.")
            return ""

    def translate_many(self, items: list[tuple[str, str]]) -> list[str]:
        # Simple, robust per-item calls. A future optimisation could batch many
        # fragments into one numbered prompt to cut request count.
        return [self.translate(han, ctx) for han, ctx in items]


register("api", GeminiTranslator)
