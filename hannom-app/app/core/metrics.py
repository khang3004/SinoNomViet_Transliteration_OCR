"""Character-level accuracy between two transcriptions.

Two denominators are reported, deliberately:

* ``cer_accuracy`` = ``1 - distance / len(reference)`` — the standard CER
  convention. This is the number to quote outside the project.
* ``max_accuracy`` = ``1 - distance / max(len(a), len(b))`` — the convention the
  upstream team's ``Task.xlsx`` uses.

They diverge exactly where it matters. When a model hallucinates extra text the
hypothesis is longer than the reference, so ``max`` divides by the larger number
and reports a *higher* score than CER for the same mistake. Reporting only the
generous one would flatter the very failure mode this audit exists to find.

Whitespace is stripped before comparison by default: in Hán-Nôm transcription
line breaks record layout, not content, and reviewers should not be penalised
for disagreeing about where a column ends.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WS = re.compile(r"\s+")


def normalize(text: str, *, strip_whitespace: bool = True) -> str:
    """NFC-fold and optionally drop all whitespace.

    NFC matters for CJK: the same character can arrive decomposed or as a
    compatibility variant, and comparing those raw counts as an edit that no
    human would call an error.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFC", str(text))
    if strip_whitespace:
        return _WS.sub("", folded)
    return folded.strip()


def levenshtein(a: str, b: str) -> int:
    """Edit distance, one rolling row of memory.

    A full matrix for two 2,000-character inscriptions is four million cells;
    the rolling row is two thousand. Same result.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Iterate over the shorter string so the row stays small.
    if len(a) < len(b):
        a, b = b, a

    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        for j, ch_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,          # deletion
                    current[j - 1] + 1,       # insertion
                    previous[j - 1] + (ch_a != ch_b),  # substitution
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class Similarity:
    """How far a hypothesis sits from a reference."""

    distance: int
    reference_len: int
    hypothesis_len: int
    cer_accuracy: float
    max_accuracy: float

    @property
    def exact(self) -> bool:
        return self.distance == 0

    def to_json(self) -> dict[str, object]:
        return {
            "distance": self.distance,
            "reference_len": self.reference_len,
            "hypothesis_len": self.hypothesis_len,
            "cer_accuracy": round(self.cer_accuracy, 4),
            "max_accuracy": round(self.max_accuracy, 4),
        }


def compare(reference: str, hypothesis: str, *, strip_whitespace: bool = True) -> Similarity:
    """Accuracy of ``hypothesis`` measured against ``reference``.

    Both empty counts as a perfect match; one empty counts as a total miss.
    Scores are clamped to [0, 1] — a hypothesis three times the reference length
    otherwise yields a negative CER, which reads as a bug in every chart.
    """
    ref = normalize(reference, strip_whitespace=strip_whitespace)
    hyp = normalize(hypothesis, strip_whitespace=strip_whitespace)

    if not ref and not hyp:
        return Similarity(0, 0, 0, 1.0, 1.0)

    distance = levenshtein(ref, hyp)
    longest = max(len(ref), len(hyp))
    cer = 1.0 - distance / len(ref) if ref else 0.0
    return Similarity(
        distance=distance,
        reference_len=len(ref),
        hypothesis_len=len(hyp),
        cer_accuracy=min(1.0, max(0.0, cer)),
        max_accuracy=min(1.0, max(0.0, 1.0 - distance / longest)) if longest else 1.0,
    )
