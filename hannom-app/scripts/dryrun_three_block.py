"""Regression dry-run (AGENTS.md §11.5) — ported handlers vs the ORIGINAL engine.

Proves the port did NOT change existing output. The ``han_only`` and
``three_block`` handlers wrap a vendored copy of the spatial layout engine; this
script imports the ORIGINAL engine from ``src/sinonom_ocr`` and asserts the
ported path produces byte-identical column groupings on the same mock input.

No GPU, no Paddle. Read-only against ``src/`` (imports it, never modifies it).

Run:  python -m scripts.dryrun_three_block   (from hannom-app/)
"""

from __future__ import annotations

import json
import os
import sys

try:  # ensure CJK prints on Windows consoles (cp1252/cp1258 default)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# Make the outer repo's src/ importable (read-only) for the original engine.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from sinonom_ocr import spatial_layout_engine as orig  # ORIGINAL (src/)

from pipeline import layouts
from pipeline.config import load_config
from pipeline.layouts._spatial import detections_to_boxes, process_page_layout
from pipeline.page_context import PageContext


def _boxes_to_detections(boxes) -> list[dict]:
    """Convert original-engine BoundingBox objects to common detection dicts."""
    return [
        {"text": b.text, "bbox": [b.x_min, b.y_min, b.x_max, b.y_max], "conf": b.confidence}
        for b in boxes
    ]


def main() -> int:
    print("=" * 72)
    print("REGRESSION DRY-RUN: ported handlers vs ORIGINAL src/ engine (no GPU)")
    print("=" * 72)

    # Same mock input for both paths (from the original engine's own helper).
    orig_boxes = orig.create_mock_multi_char_response()
    detections = _boxes_to_detections(orig_boxes)

    # ORIGINAL engine column texts.
    orig_cols, _ = orig.process_page_layout(orig.create_mock_multi_char_response())
    orig_texts = [c.full_text() for c in orig_cols]

    # VENDORED engine column texts (used by the ported handlers).
    vend_cols, _ = process_page_layout(detections_to_boxes(detections))
    vend_texts = [c.full_text() for c in vend_cols]

    print(f"Original  columns: {orig_texts}")
    print(f"Vendored  columns: {vend_texts}")
    assert orig_texts == vend_texts, "PORT CHANGED OUTPUT: vendored != original engine!"
    print("✅ vendored engine output is IDENTICAL to the original src/ engine.\n")

    cfg = load_config()

    # han_only handler — direct port; column texts must match the original.
    ctx_h = PageContext(source_doc="AnNam", page=1, config=cfg, mock_han_ocr=detections)
    han_handler = layouts.get_handler("han_only")
    han_records = han_handler.extract(ctx_h)
    han_texts = [r.han for r in han_records]
    print(f"han_only handler 'han' fields: {han_texts}")
    assert han_texts == orig_texts, "han_only handler diverged from original engine!"
    print("✅ han_only handler reproduces the original column grouping.\n")

    # three_block handler — same column engine, then split into 3 y-bands.
    ctx_t = PageContext(source_doc="UcTraiTap", page=1, config=cfg, mock_han_ocr=detections)
    tb_handler = layouts.route(ctx_t)
    assert tb_handler.name == "three_block", f"router picked {tb_handler.name!r}, not three_block"
    tb_records = tb_handler.extract(ctx_t)
    # The three bands recombined per column must equal the original column text.
    tb_recombined = [r.han + r.phonetic + r.meaning for r in tb_records]
    print(f"three_block recombined per column: {tb_recombined}")
    assert tb_recombined == orig_texts, "three_block lost/altered characters vs original!"
    print("✅ three_block handler preserves all characters (no column-grouping change).\n")

    print("Sample three_block record:")
    print(json.dumps(tb_records[0].to_dict(), ensure_ascii=False, indent=2))

    print("\n✅ REGRESSION PASSED: porting did not change existing engine output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
