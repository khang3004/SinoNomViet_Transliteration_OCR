"""LLM provider protocol (multi-provider, bring-your-own-key).

A provider is a thin client over one vendor (Gemini / OpenAI / Anthropic). The
API key is passed PER CALL — never read from the environment, never stored — so
each user supplies their own key. Used for translation & Han correction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """One vendor's chat/completion client."""

    name: str
    default_model: str
    default_vision_model: str
    supports_vision: bool
    suggested_models: list[str]  # newest first — offered in the UI model picker

    def complete(
        self, prompt: str, api_key: str, model: str | None = None, system: str | None = None
    ) -> str:
        """Return the model's text response for ``prompt`` using ``api_key``."""
        ...

    def complete_vision(
        self,
        prompt: str,
        image_bytes: bytes,
        api_key: str,
        model: str | None = None,
        system: str | None = None,
    ) -> str:
        """Return the model's text response for ``prompt`` + a PNG image.

        Used to read Han characters straight from a cropped page region when OCR
        is wrong or missing. ``image_bytes`` is raw PNG bytes.
        """
        ...
