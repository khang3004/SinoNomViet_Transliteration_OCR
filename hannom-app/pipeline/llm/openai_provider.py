"""OpenAI (ChatGPT) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class OpenAIProvider:
    name = "openai"
    default_model = "gpt-5"
    default_vision_model = "gpt-5"  # multimodal
    supports_vision = True
    suggested_models = ["gpt-5.6", "gpt-5.5", "gpt-5", "gpt-5-mini"]

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

    def complete_vision(self, prompt, images, api_key, model=None, system=None) -> str:
        import base64

        from openai import OpenAI  # lazy

        client = OpenAI(api_key=api_key)
        content = [{"type": "text", "text": prompt}]
        for img in images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        resp = client.chat.completions.create(
            model=model or self.default_vision_model, messages=messages, temperature=0
        )
        return (resp.choices[0].message.content or "").strip()


register(OpenAIProvider())
