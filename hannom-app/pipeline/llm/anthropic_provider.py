"""Anthropic (Claude) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class AnthropicProvider:
    name = "anthropic"
    default_model = "claude-3-5-haiku-latest"
    # 3.5 Haiku is text-only; use a vision-capable model for image reads.
    default_vision_model = "claude-3-5-sonnet-latest"
    supports_vision = True

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

    def complete_vision(self, prompt, image_bytes, api_key, model=None, system=None) -> str:
        import base64

        import anthropic  # lazy

        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        msg = client.messages.create(
            model=model or self.default_vision_model,
            max_tokens=1024,
            system=system or "",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "".join(parts).strip()


register(AnthropicProvider())
