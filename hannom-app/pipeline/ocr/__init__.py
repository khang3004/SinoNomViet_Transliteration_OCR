"""OCR engine registry (AGENTS.md §3.1).

Engines register themselves by name. The active engine is selected via the
``OCR_BACKEND`` env var. Adding an engine later is a single ``register(...)``
call in a new file — no existing code changes.

    from pipeline.ocr import register, get_engine
    register("myengine", MyEngineClass)
    engine = get_engine("myengine")
"""

from __future__ import annotations

import logging
from typing import Callable

from pipeline.ocr.base import Detection, OCREngine

logger = logging.getLogger("hannom.ocr")

# name -> zero-arg factory returning an OCREngine instance.
_REGISTRY: dict[str, Callable[[], OCREngine]] = {}


def register(name: str, factory: Callable[[], OCREngine]) -> None:
    """Register an OCR engine factory under ``name``.

    ``factory`` is a zero-arg callable (usually the engine class) so that heavy
    engines (Paddle, Vision) are only constructed when actually selected.
    """
    key = name.lower()
    if key in _REGISTRY:
        logger.warning("OCR engine %r already registered; overwriting.", key)
    _REGISTRY[key] = factory
    logger.debug("Registered OCR engine %r.", key)


def available() -> list[str]:
    """Return the sorted names of all registered engines."""
    return sorted(_REGISTRY)


def get_engine(name: str) -> OCREngine:
    """Instantiate and return the engine registered under ``name``.

    Raises:
        KeyError: if no engine is registered under that name.
    """
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown OCR_BACKEND {name!r}. Registered engines: {available()}"
        )
    return _REGISTRY[key]()


# --- built-in engine registrations ----------------------------------------
# Imported for their register() side effects. Each module guards its heavy
# imports so importing this package never pulls in Paddle/Vision unless used.
from pipeline.ocr import kandianguji as _kandianguji  # noqa: E402,F401
from pipeline.ocr import mock as _mock  # noqa: E402,F401
from pipeline.ocr import paddle as _paddle  # noqa: E402,F401
from pipeline.ocr import vision as _vision  # noqa: E402,F401

__all__ = ["register", "available", "get_engine", "OCREngine", "Detection"]
