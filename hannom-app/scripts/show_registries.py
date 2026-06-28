"""Registry demonstration (AGENTS.md §11.6) — prove single-call extensibility.

Prints the OCR-engine and layout-handler registries, then demonstrates that a
brand-new engine/layout can be added with a SINGLE ``register(...)`` call at
runtime — no existing code touched.

Also confirms no secret values are logged (only key-present booleans).

Run:  python -m scripts.show_registries
"""

from __future__ import annotations

import sys

try:  # ensure CJK prints on Windows consoles (cp1252/cp1258 default)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from pipeline import layouts, ocr, translate
from pipeline.config import load_config
from pipeline.ocr.base import Detection


def main() -> int:
    print("=" * 72)
    print("OCR ENGINE REGISTRY")
    print("=" * 72)
    print("Registered OCR engines:", ocr.available())

    print("\n-- adding a NEW engine = one register() call, no existing code touched --")

    class HelloEngine:
        name = "hello"

        def ocr(self, image):  # noqa: ARG002
            return [Detection(text="你好", bbox=[0, 0, 10, 10], conf=0.99)]

    ocr.register("hello", HelloEngine)
    print("  After ocr.register('hello', HelloEngine):", ocr.available())
    print("  Newly-added engine output:", ocr.get_engine("hello").ocr("x"))

    print("\n" + "=" * 72)
    print("LAYOUT HANDLER REGISTRY (router/priority order)")
    print("=" * 72)
    print("Registered layout handlers:", layouts.available())
    for name in layouts.available():
        h = layouts.get_handler(name)
        print(f"  - {name:<12} priority={h.priority}")

    print("\n-- adding a NEW layout = one register() call --")

    class BlankHandler:
        name = "blank"
        priority = 99

        def detect(self, page_ctx):  # noqa: ARG002
            return False

        def extract(self, page_ctx):  # noqa: ARG002
            return []

    layouts.register(BlankHandler())
    print("  After layouts.register(BlankHandler()):", layouts.available())

    print("\n" + "=" * 72)
    print("TRANSLATION BACKEND REGISTRY")
    print("=" * 72)
    print("Registered translators:", translate.available())
    print("  api=Gemini flash (default, needs GOOGLE_API_KEY) | offline=Qwen stub | skip=no-op")

    print("\n" + "=" * 72)
    print("SECRET HANDLING (startup logs booleans only — never values)")
    print("=" * 72)
    cfg = load_config()
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg.log_key_presence()
    print("  Non-secret config summary:", cfg.summary())
    print("\n✅ registries + secret handling OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
