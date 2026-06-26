"""Translation backend registry (AGENTS.md §6).

Selected via ``TRANSLATE_BACKEND``. Default ``api`` = Gemini flash (cheap); the
6 GB GTX 2060 cannot host OCR + a 3B LLM together, so offline LLM translation is
NOT the default and stays behind the ``offline`` flag for bigger GPUs.

    from pipeline import translate
    translator = translate.get_translator(config)
    vi = translator.translate("平定營公堂官")
"""

from __future__ import annotations

import logging
from typing import Callable

from pipeline.translate.base import Translator

logger = logging.getLogger("hannom.translate")

# name -> factory(config) -> Translator
_REGISTRY: dict[str, Callable[[object], Translator]] = {}


def register(name: str, factory: Callable[[object], Translator]) -> None:
    """Register a translator factory under ``name`` (factory takes the Config)."""
    key = name.lower()
    if key in _REGISTRY:
        logger.warning("Translator %r already registered; overwriting.", key)
    _REGISTRY[key] = factory
    logger.debug("Registered translator %r.", key)


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_translator(config) -> Translator:
    """Instantiate the translator named by ``config.translate_backend``."""
    name = getattr(config, "translate_backend", "skip")
    key = str(name).lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown TRANSLATE_BACKEND {name!r}. Registered: {available()}"
        )
    return _REGISTRY[key](config)


# --- built-in registrations (side-effect imports) -------------------------
from pipeline.translate import gemini as _gemini  # noqa: E402,F401
from pipeline.translate import mock as _mock  # noqa: E402,F401
from pipeline.translate import offline_qwen as _offline  # noqa: E402,F401
from pipeline.translate import skip as _skip  # noqa: E402,F401

__all__ = ["register", "available", "get_translator", "Translator"]
