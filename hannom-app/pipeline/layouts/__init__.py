"""Layout handler registry + router (AGENTS.md §3.2).

Handlers register themselves by name with a priority. The router tries them in
priority order and returns the first whose ``detect()`` is true. ``two_column``
(PRIMARY) has the lowest priority number, so it is checked FIRST.

    from pipeline.layouts import register, route
    handler = route(page_ctx)        # picks the matching handler
    records = handler.extract(page_ctx)
"""

from __future__ import annotations

import logging

from pipeline.layouts.base import LayoutHandler
from pipeline.page_context import PageContext

logger = logging.getLogger("hannom.layouts")

# name -> handler instance.
_REGISTRY: dict[str, LayoutHandler] = {}


def register(handler: LayoutHandler) -> None:
    """Register a layout handler instance (idempotent per name)."""
    name = handler.name
    if name in _REGISTRY:
        logger.warning("Layout handler %r already registered; overwriting.", name)
    _REGISTRY[name] = handler
    logger.debug("Registered layout handler %r (priority=%s).", name, handler.priority)


def available() -> list[str]:
    """Return handler names in router (priority) order."""
    return [h.name for h in _ordered()]


def get_handler(name: str) -> LayoutHandler:
    """Return the handler registered under ``name``."""
    if name not in _REGISTRY:
        raise KeyError(f"Unknown layout {name!r}. Registered: {list(_REGISTRY)}")
    return _REGISTRY[name]


def _ordered() -> list[LayoutHandler]:
    return sorted(_REGISTRY.values(), key=lambda h: (h.priority, h.name))


def route(page_ctx: PageContext) -> LayoutHandler:
    """Return the first handler (priority order) whose ``detect()`` is true.

    Raises:
        RuntimeError: if no registered handler matches the page.
    """
    for handler in _ordered():
        try:
            if handler.detect(page_ctx):
                logger.info("Router selected layout handler %r.", handler.name)
                return handler
        except Exception:  # noqa: BLE001 - a broken detector must not kill routing
            logger.exception("detect() raised in handler %r; skipping.", handler.name)
    raise RuntimeError(
        "No layout handler matched the page. Registered handlers: "
        + ", ".join(available())
    )


# --- built-in handler registrations ---------------------------------------
from pipeline.layouts import han_only as _han_only  # noqa: E402,F401
from pipeline.layouts import three_block as _three_block  # noqa: E402,F401
from pipeline.layouts import two_column as _two_column  # noqa: E402,F401

__all__ = ["register", "available", "get_handler", "route", "LayoutHandler"]
