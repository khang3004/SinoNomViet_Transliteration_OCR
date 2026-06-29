"""``offline`` corrector — local LLM proofreading STUB (Bug 3).

Behind ``CORRECT_BACKEND=offline``; not the default. Like offline translation,
a local LLM proofreading pass is for bigger GPUs and is left as a clear
extension point that fails loudly rather than silently OOM on the 2060.
"""

from __future__ import annotations

from pipeline.correct import register


class OfflineCorrector:
    name = "offline"

    def __init__(self, config=None) -> None:  # noqa: ARG002
        pass

    def correct(self, han: str, context: str = "") -> str:  # noqa: ARG002
        raise NotImplementedError(
            "Offline LLM Han correction is a stub (CORRECT_BACKEND=offline is for "
            "bigger GPUs). Use CORRECT_BACKEND=dict (dictionary S1∩S2) or =api "
            "(Gemini), or =skip (default)."
        )


register("offline", OfflineCorrector)
