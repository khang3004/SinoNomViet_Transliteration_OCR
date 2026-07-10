"""Gemini provider on the current **google-genai** SDK. Key passed per call.

Uses ``from google import genai`` (the new SDK that replaces google-generativeai),
so we can reach the newest models/features. The API key is still supplied PER CALL
(each reviewer's own key) — never read from the environment, never stored.
"""

from __future__ import annotations

from pipeline.llm import register

# 2-image vision calls can be slow; give the HTTP call plenty of room (milliseconds).
_TIMEOUT_MS = 300_000
_TEMPERATURE = 0.2  # deterministic transcription


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

        resp = self._client(api_key).models.generate_content(
            model=model or self.default_model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=_TEMPERATURE),
        )
        return (resp.text or "").strip()

    def complete_vision(self, prompt, images, api_key, model=None, system=None) -> str:
        from google.genai import types

        contents = [prompt] + [
            types.Part.from_bytes(data=img, mime_type="image/png") for img in images
        ]
        resp = self._client(api_key).models.generate_content(
            model=model or self.default_vision_model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system, temperature=_TEMPERATURE),
        )
        return (resp.text or "").strip()


register(GeminiProvider())
