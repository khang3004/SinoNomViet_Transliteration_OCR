"""``skip`` translator — no-op (AGENTS.md §6, default for CORRECT_BACKEND).

Used when translation is disabled, or when the meaning already comes from a
higher-trust source (e.g. the two_column PDF text layer) and must not be
overwritten. Returns empty strings and never touches the network.
"""

from __future__ import annotations

from pipeline.translate import register


class SkipTranslator:
    name = "skip"
    source_tag = ""

    def __init__(self, config=None) -> None:  # noqa: ARG002
        pass

    def translate(self, han: str, context: str = "") -> str:  # noqa: ARG002
        return ""

    def translate_many(self, items: list[tuple[str, str]]) -> list[str]:
        return ["" for _ in items]


register("skip", SkipTranslator)
