"""Han OCR correction registry (Bug 3).

Selected via ``CORRECT_BACKEND``. Default ``skip`` (no-op) so correction is
opt-in and never changes existing output unless explicitly enabled.

    from pipeline import correct
    corrector = correct.get_corrector(config)
    fixed = corrector.correct("拜調")
"""

from __future__ import annotations

import logging
from typing import Callable

from pipeline.correct.base import Corrector

logger = logging.getLogger("hannom.correct")

# name -> factory(config) -> Corrector
_REGISTRY: dict[str, Callable[[object], Corrector]] = {}


def register(name: str, factory: Callable[[object], Corrector]) -> None:
    key = name.lower()
    if key in _REGISTRY:
        logger.warning("Corrector %r already registered; overwriting.", key)
    _REGISTRY[key] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_corrector(config) -> Corrector:
    name = getattr(config, "correct_backend", "skip")
    key = str(name).lower()
    if key not in _REGISTRY:
        raise KeyError(f"Unknown CORRECT_BACKEND {name!r}. Registered: {available()}")
    return _REGISTRY[key](config)


# --- built-in registrations (side-effect imports) -------------------------
from pipeline.correct import api as _api  # noqa: E402,F401
from pipeline.correct import dict_corrector as _dict  # noqa: E402,F401
from pipeline.correct import offline_corrector as _offline  # noqa: E402,F401
from pipeline.correct import skip as _skip  # noqa: E402,F401

__all__ = ["register", "available", "get_corrector", "Corrector"]
