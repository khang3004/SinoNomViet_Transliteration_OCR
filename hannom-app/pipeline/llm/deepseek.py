"""DeepSeek provider. Key passed per call.

DeepSeek's chat API is OpenAI-compatible, so we reuse the ``openai`` SDK pointed at
DeepSeek's base URL (no extra dependency). DeepSeek has no public multimodal model,
so vision is unsupported — use Gemini/OpenAI/Anthropic for the image button.
"""

from __future__ import annotations

from pipeline.llm import register

_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider:
    name = "deepseek"
    default_model = "deepseek-chat"
    default_vision_model = ""       # no vision model
    supports_vision = False

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        from openai import OpenAI  # lazy — DeepSeek speaks the OpenAI protocol

        client = OpenAI(api_key=api_key, base_url=_BASE_URL)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=model or self.default_model, messages=messages, temperature=0
        )
        return (resp.choices[0].message.content or "").strip()

    def complete_vision(self, prompt, image_bytes, api_key, model=None, system=None) -> str:
        raise ValueError(
            "DeepSeek has no image/vision model — use Gemini, OpenAI, or Anthropic "
            "for reading Hán from an image."
        )


register(DeepSeekProvider())
