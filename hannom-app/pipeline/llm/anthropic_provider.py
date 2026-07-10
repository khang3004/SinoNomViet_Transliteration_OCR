"""Anthropic (Claude) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class AnthropicProvider:
    name = "anthropic"
    default_model = "claude-sonnet-4-6"
    default_vision_model = "claude-sonnet-4-6"  # multimodal
    supports_vision = True
    suggested_models = [
        "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-fable-5",
    ]

    def complete(self, prompt, api_key, model=None, system=None) -> str:
        import anthropic  # lazy

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model or self.default_model,
            max_tokens=1024,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "".join(parts).strip()

    def complete_vision(self, prompt, images, api_key, model=None, system=None) -> str:
        import base64

        import anthropic  # lazy

        client = anthropic.Anthropic(api_key=api_key)
        content = []
        for img in images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
        content.append({"type": "text", "text": prompt})
        msg = client.messages.create(
            model=model or self.default_vision_model,
            max_tokens=1024,
            system=system or "",
            messages=[{"role": "user", "content": content}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "".join(parts).strip()


register(AnthropicProvider())
