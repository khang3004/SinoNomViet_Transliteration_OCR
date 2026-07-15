from __future__ import annotations

import re

from common.text_script import script_ratios

# Suspicious repeated noise patterns from bad OCR / Mẫu nhiễu lặp từ OCR lỗi
_GARBAGE_PATTERN = re.compile(r"(.)\1{5,}")


def estimate_gemini_ocr_confidence(text: str) -> float:
    """Estimate OCR quality when the vision API returns no logprobs.

    Ước lượng chất lượng OCR khi API vision không trả logprobs.
  """
    cleaned = text.strip()
    if not cleaned:
        return 0.0

    compact = "".join(character for character in cleaned if not character.isspace())
    if not compact:
        return 0.0

    _cjk_ratio, latin_ratio, total = script_ratios(cleaned)
    script_ratio = max(_cjk_ratio, latin_ratio)

    printable_ratio = sum(1 for character in compact if character.isprintable()) / total
    unique_ratio = len(set(compact)) / total
    garbage_hits = len(_GARBAGE_PATTERN.findall(compact))

    lines = [line for line in cleaned.splitlines() if line.strip()]
    line_score = min(1.0, len(lines) / 4.0) if lines else 0.25
    length_score = min(1.0, len(compact) / 120.0)
    garbage_penalty = min(0.35, garbage_hits * 0.12)

    score = (
        0.22 * printable_ratio
        + 0.18 * min(1.0, unique_ratio * 1.8)
        + 0.22 * length_score
        + 0.18 * line_score
        + 0.20 * min(1.0, script_ratio * 1.2)
        - garbage_penalty
    )
    return round(min(0.99, max(0.05, score)), 4)
