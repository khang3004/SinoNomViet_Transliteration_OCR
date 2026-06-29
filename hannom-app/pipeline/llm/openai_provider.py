"""OpenAI (ChatGPT) provider. Key passed per call."""

from __future__ import annotations

from pipeline.llm import register


class OpenAIProvider:
    name = "openai"
    default_model = "gpt-4o-mini"

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


register(OpenAIProvider())
