"""OpenAI (ChatGPT) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class OpenAIProvider:
    name = "openai"
    default_model = "gpt-4o-mini"
    default_vision_model = "gpt-4o-mini"  # multimodal

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        from openai import OpenAI  # lazy

        client = OpenAI(api_key=api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=model or self.default_model, messages=messages, temperature=0
        )
        return (resp.choices[0].message.content or "").strip()

    def complete_vision(self, prompt, image_bytes, api_key, model=None, system=None) -> str:
        import base64

        from openai import OpenAI  # lazy

        client = OpenAI(api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]})
        resp = client.chat.completions.create(
            model=model or self.default_vision_model, messages=messages, temperature=0
        )
        return (resp.choices[0].message.content or "").strip()


register(OpenAIProvider())
