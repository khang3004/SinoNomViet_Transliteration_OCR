"""Gemini provider on the current **google-genai** SDK. Key passed per call.

Uses ``from google import genai`` (the new SDK that replaces google-generativeai),
so we can reach the newest models/features. The API key is still supplied PER CALL
(each reviewer's own key) — never read from the environment, never stored.
"""

from __future__ import annotations

import logging
import time

from pipeline.llm import register

logger = logging.getLogger("hannom.llm.gemini")

# 2-image vision calls can be slow; give the HTTP call plenty of room (milliseconds).
_TIMEOUT_MS = 300_000
_TEMPERATURE = 0.2  # deterministic transcription

# Gemini returns 503 UNAVAILABLE / 429 when a model is briefly overloaded. These
# are transient, so retry with exponential backoff before surfacing to the user.
_RETRY_MARKERS = ("503", "unavailable", "overloaded", "429", "resource_exhausted", "high demand")
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 2.0


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 503):
        return True
    return any(m in msg for m in _RETRY_MARKERS)


def _with_retry(call):
    """Run ``call``; retry transient overload errors with exponential backoff."""
    last = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - decide retry vs. raise by content
            if not _is_transient(exc) or attempt == _MAX_ATTEMPTS:
                raise
            last = exc
            delay = _BACKOFF_BASE_S * (2 ** (attempt - 1))
            logger.warning("Gemini transient error (attempt %d/%d), retrying in %.0fs: %s",
                           attempt, _MAX_ATTEMPTS, delay, exc)
            time.sleep(delay)
    raise last  # unreachable, but keeps type-checkers happy


class GeminiProvider:
    name = "gemini"
    # Alias that always points at the newest Flash — avoids 404s when Google
    # retires a dated model (e.g. gemini-2.0-flash, shut down 2026-06).
    default_model = "gemini-flash-latest"
    default_vision_model = "gemini-flash-latest"  # multimodal
    supports_vision = True
    suggested_models = [
        "gemini-flash-latest", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash",
    ]

    def _client(self, api_key):
        from google import genai
        from google.genai import types

        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS))

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        from google.genai import types

        resp = _with_retry(lambda: self._client(api_key).models.generate_content(
            model=model or self.default_model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=_TEMPERATURE),
        ))
        return (resp.text or "").strip()

    def complete_vision(self, prompt, images, api_key, model=None, system=None) -> str:
        from google.genai import types

        contents = [prompt] + [
            types.Part.from_bytes(data=img, mime_type="image/png") for img in images
        ]
        resp = _with_retry(lambda: self._client(api_key).models.generate_content(
            model=model or self.default_vision_model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system, temperature=_TEMPERATURE),
        ))
        return (resp.text or "").strip()


register(GeminiProvider())
