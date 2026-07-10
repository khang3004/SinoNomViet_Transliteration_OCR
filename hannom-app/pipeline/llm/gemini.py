"""Gemini provider (google-generativeai). Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class GeminiProvider:
    name = "gemini"
    # Alias that always points at the newest Flash (currently 3.5) — avoids 404s
    # when Google retires a dated model (e.g. gemini-2.0-flash, shut down 2026-06).
    default_model = "gemini-flash-latest"
    default_vision_model = "gemini-flash-latest"  # multimodal
    supports_vision = True
    suggested_models = [
        "gemini-flash-latest", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash",
    ]

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        import google.generativeai as genai  # lazy

        genai.configure(api_key=api_key)
        gm = genai.GenerativeModel(model or self.default_model, system_instruction=system)
        resp = gm.generate_content(prompt)
        return (resp.text or "").strip()

    def complete_vision(self, prompt, image_bytes, api_key, model=None, system=None) -> str:
        import google.generativeai as genai  # lazy

        genai.configure(api_key=api_key)
        gm = genai.GenerativeModel(model or self.default_vision_model, system_instruction=system)
        resp = gm.generate_content([prompt, {"mime_type": "image/png", "data": image_bytes}])
        return (resp.text or "").strip()


register(GeminiProvider())
