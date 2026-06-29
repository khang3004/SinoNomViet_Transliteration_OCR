"""Gemini provider (google-generativeai). Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class GeminiProvider:
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        import google.generativeai as genai  # lazy

        genai.configure(api_key=api_key)
        gm = genai.GenerativeModel(model or self.default_model, system_instruction=system)
        resp = gm.generate_content(prompt)
        return (resp.text or "").strip()


register(GeminiProvider())
