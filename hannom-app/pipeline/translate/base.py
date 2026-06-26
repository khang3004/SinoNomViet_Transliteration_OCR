"""Translator protocol (AGENTS.md §6).

A translator turns a Han / Hán-Nôm string into a modern Vietnamese (Quốc Ngữ)
meaning. Backends are selected via ``TRANSLATE_BACKEND`` and registered the same
way OCR engines are — adding one later is a single ``register(...)`` call.

``source_tag`` is written into ``record.source_of.meaning`` so each line records
WHERE its meaning came from (e.g. ``"gemini"``), mirroring ``"pdf_text"`` for the
two_column layout.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Translator(Protocol):
    """Protocol all translation backends implement."""

    name: str
    source_tag: str  # provenance written to source_of.meaning

    def translate(self, han: str, context: str = "") -> str:
        """Translate one Han string to Vietnamese. ``context`` is optional hint."""
        ...

    def translate_many(self, items: list[tuple[str, str]]) -> list[str]:
        """Translate a batch of ``(han, context)`` pairs (order preserved)."""
        ...
