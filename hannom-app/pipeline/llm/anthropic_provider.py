"""Anthropic (Claude) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class AnthropicProvider:
    name = "anthropic"
    default_model = "claude-3-5-haiku-latest"

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


register(AnthropicProvider())
