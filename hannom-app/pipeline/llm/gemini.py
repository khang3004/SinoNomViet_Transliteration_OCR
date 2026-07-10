"""Gemini provider (google-generativeai). Key passed per call."""

from __future__ import annotations

from pipeline.llm import register

_TIMEOUT = 300  # seconds — vision calls with 2 images can be slow; avoid gRPC deadline 504s


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
        resp = gm.generate_content(prompt, generation_config={"temperature": 0.2}, request_options={"timeout": _TIMEOUT})
        return (resp.text or "").strip()

    def complete_vision(self, prompt, images, api_key, model=None, system=None) -> str:
        import google.generativeai as genai  # lazy

        genai.configure(api_key=api_key)
        gm = genai.GenerativeModel(model or self.default_vision_model, system_instruction=system)
        parts = [prompt] + [{"mime_type": "image/png", "data": img} for img in images]
        resp = gm.generate_content(parts, generation_config={"temperature": 0.2}, request_options={"timeout": _TIMEOUT})
        return (resp.text or "").strip()


register(GeminiProvider())
