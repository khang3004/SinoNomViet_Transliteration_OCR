"""Mock translator — local testing WITHOUT a GOOGLE_API_KEY (AGENTS.md §11).

Not a production backend; a developer convenience (like the mock OCR engine) so
the full pipeline can demonstrate meaning-filling end-to-end with no network.
Selected via ``TRANSLATE_BACKEND=mock``. Returns a deterministic placeholder
gloss so you can see records get a non-empty ``meaning`` and the right provenance.
"""

from __future__ import annotations

from pipeline.translate import register


class MockTranslator:
    name = "mock"
    source_tag = "mock_mt"

    def __init__(self, config=None) -> None:  # noqa: ARG002
        pass

    def translate(self, han: str, context: str = "") -> str:  # noqa: ARG002
        han = (han or "").strip()
        return f"[VI-mock] dịch nghĩa của: {han}" if han else ""

    def translate_many(self, items: list[tuple[str, str]]) -> list[str]:
        return [self.translate(h, c) for h, c in items]


register("mock", MockTranslator)
