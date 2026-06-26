"""Layout handler protocol + shared text helpers (AGENTS.md §3.2).

A handler answers two questions about a :class:`PageContext`:
  - ``detect(page_ctx) -> bool``     : does this handler apply to the page?
  - ``extract(page_ctx) -> list[Record]`` : produce parallel records.

Handlers are tried by the router in priority order (``two_column`` first).
Adding a doc type later = a new file here + one ``register(...)`` call.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.page_context import PageContext
from pipeline.schema import Record


@runtime_checkable
class LayoutHandler(Protocol):
    """Protocol every layout handler implements."""

    name: str
    priority: int  # lower = checked earlier by the router

    def detect(self, page_ctx: PageContext) -> bool:
        """Return True if this handler should process ``page_ctx``."""
        ...

    def extract(self, page_ctx: PageContext) -> list[Record]:
        """Produce parallel records from ``page_ctx``."""
        ...


# --- shared text helpers ---------------------------------------------------

# CJK Unified Ideographs (+ Extension A) and common compatibility ranges.
_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK Ext A
    (0x4E00, 0x9FFF),  # CJK Unified
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # CJK Ext B
)


def is_cjk(ch: str) -> bool:
    """True if ``ch`` is a single CJK ideograph."""
    if not ch:
        return False
    cp = ord(ch[0])
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_ratio(text: str) -> float:
    """Fraction of characters in ``text`` that are CJK ideographs."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(is_cjk(c) for c in chars) / len(chars)


def filter_han_detections(
    detections: list[dict],
    min_conf: float = 0.5,
    min_cjk_ratio: float = 0.5,
) -> list[dict]:
    """Drop watermark / noise tokens from a Han OCR result (AGENTS.md §4.6).

    A detection is dropped when it is BOTH low-confidence AND mostly non-CJK —
    this kills watermark bleed like ``"LƯU TRỮ VN"`` without over-filtering real
    Han (high-confidence Han is always kept; a confident CJK token is kept even
    if short).

    Args:
        detections:    list of {text, bbox, conf}.
        min_conf:      confidence below this is "low".
        min_cjk_ratio: CJK fraction below this is "mostly non-CJK".
    """
    kept: list[dict] = []
    for det in detections:
        text = det.get("text", "")
        conf = float(det.get("conf", 0.0))
        ratio = cjk_ratio(text)
        is_noise = conf < min_conf and ratio < min_cjk_ratio
        if not is_noise:
            kept.append(det)
    return kept
