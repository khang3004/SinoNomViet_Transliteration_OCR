"""Offline LLM translation — ``TRANSLATE_BACKEND=offline`` (AGENTS.md §2, §6).

Behind a flag, NOT the default: a local Qwen model (``QWEN_MODEL``,
default ``Qwen2.5-3B-Instruct``). The 6 GB GTX 2060 CANNOT host OCR + a 3B LLM
together, so this is for people with bigger GPUs only. Left as a clear
extension point — the loader is stubbed to fail loudly rather than silently OOM.

TODO(offline-llm): load the model with transformers and generate the Vietnamese
meaning. Guard memory so it never competes with OCR on a small GPU.
"""

from __future__ import annotations

import os

from pipeline.translate import register


class OfflineQwenTranslator:
    name = "offline"
    source_tag = "qwen"

    def __init__(self, config=None) -> None:
        self._model_id = (
            getattr(config, "qwen_model", None)
            or os.environ.get("QWEN_MODEL", "Qwen2.5-3B-Instruct")
        )

    def translate(self, han: str, context: str = "") -> str:  # noqa: ARG002
        raise NotImplementedError(
            f"Offline LLM translation ({self._model_id}) is a stub. It is behind "
            "the TRANSLATE_BACKEND=offline flag for bigger GPUs; the default 6 GB "
            "GTX 2060 cannot host OCR + a 3B LLM together. Use "
            "TRANSLATE_BACKEND=api (Gemini) instead."
        )

    def translate_many(self, items: list[tuple[str, str]]) -> list[str]:
        return [self.translate(h, c) for h, c in items]


register("offline", OfflineQwenTranslator)
