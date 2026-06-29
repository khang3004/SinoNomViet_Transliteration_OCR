"""``skip`` corrector — no-op (default). Han stays exactly as OCR'd."""

from __future__ import annotations

from pipeline.correct import register


class SkipCorrector:
    name = "skip"

    def __init__(self, config=None) -> None:  # noqa: ARG002
        pass

    def correct(self, han: str, context: str = "") -> str:  # noqa: ARG002
        return han


register("skip", SkipCorrector)
