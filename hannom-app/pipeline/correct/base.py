"""Han OCR correction protocol (Bug 3).

A corrector proofreads the OCR'd Han for an entry and returns the corrected
text. Gated by ``CORRECT_BACKEND`` (skip | dict | api | offline); default skip
is a no-op so behaviour is opt-in. The raw OCR is always preserved in
``Record.han_raw`` so reviewers can see what changed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Corrector(Protocol):
    """Protocol all Han correction backends implement."""

    name: str

    def correct(self, han: str) -> str:
        """Return corrected Han for ``han`` (same characters if no change)."""
        ...
