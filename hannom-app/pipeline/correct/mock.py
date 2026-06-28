"""``mock`` corrector — keyless demo of the correction flow (Bug 3).

Not a production backend (like the mock OCR/translation engines). It applies a
tiny, explicit confusion map so the correction pipeline can be demonstrated
end-to-end WITHOUT a Gemini key — ``han_raw`` keeps the original OCR and ``han``
shows the repaired text. Selected via ``CORRECT_BACKEND=mock``.

The map intentionally includes the real OCR confusion observed on the sample
(``調`` mis-read for ``詣``); it is illustrative only — real correction uses the
``api`` (Gemini) or ``dict`` backends.
"""

from __future__ import annotations

from pipeline.correct import register

# Illustrative shape-confusion repairs seen on Nguyễn-dynasty Châu bản OCR.
_DEMO_FIXES = {
    "調": "詣",  # 拜調 → 拜詣 (the error flagged on the sample)
    "異": "詣",
    "黨": "賞",
}


class MockCorrector:
    name = "mock"

    def __init__(self, config=None) -> None:  # noqa: ARG002
        pass

    def correct(self, han: str) -> str:
        return "".join(_DEMO_FIXES.get(ch, ch) for ch in (han or ""))


register("mock", MockCorrector)
