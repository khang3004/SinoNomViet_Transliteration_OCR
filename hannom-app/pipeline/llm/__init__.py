"""Multi-provider LLM registry (bring-your-own-key).

    from pipeline import llm
    text = llm.complete("gemini", prompt, api_key=user_key, system=SYS)

Providers register by name; each accepts the API key per call. Adding a vendor
later = a new file here + one ``register(...)`` call. Provider SDKs are imported
lazily, so importing this package never requires them.
"""

from __future__ import annotations

import logging

from pipeline.llm.base import LLMProvider

logger = logging.getLogger("hannom.llm")

_REGISTRY: dict[str, LLMProvider] = {}


def register(provider: LLMProvider) -> None:
    _REGISTRY[provider.name.lower()] = provider


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str) -> LLMProvider:
    key = (name or "").lower()
    if key not in _REGISTRY:
        raise KeyError(f"Unknown LLM provider {name!r}. Registered: {available()}")
    return _REGISTRY[key]


def complete(
    name: str, prompt: str, api_key: str, model: str | None = None, system: str | None = None
) -> str:
    """Run one completion against provider ``name`` with a per-call ``api_key``."""
    if not (api_key or "").strip():
        raise ValueError("An API key is required (paste your own provider key).")
    return get_provider(name).complete(prompt, api_key=api_key, model=model, system=system)


# --- built-in provider registrations (lazy SDK imports inside each) ---------
from pipeline.llm import anthropic_provider as _anthropic  # noqa: E402,F401
from pipeline.llm import gemini as _gemini  # noqa: E402,F401
from pipeline.llm import openai_provider as _openai  # noqa: E402,F401

__all__ = ["register", "available", "get_provider", "complete", "LLMProvider"]
