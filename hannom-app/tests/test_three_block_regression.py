"""Regression: ported handlers must match the ORIGINAL src/ engine output."""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from sinonom_ocr import spatial_layout_engine as orig  # noqa: E402

from pipeline import layouts  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.layouts._spatial import detections_to_boxes, process_page_layout  # noqa: E402
from pipeline.page_context import PageContext  # noqa: E402


def _detections():
    boxes = orig.create_mock_multi_char_response()
    return [
        {"text": b.text, "bbox": [b.x_min, b.y_min, b.x_max, b.y_max], "conf": b.confidence}
        for b in boxes
    ]


def _orig_texts():
    cols, _ = orig.process_page_layout(orig.create_mock_multi_char_response())
    return [c.full_text() for c in cols]


def test_vendored_engine_matches_original():
    vend, _ = process_page_layout(detections_to_boxes(_detections()))
    assert [c.full_text() for c in vend] == _orig_texts()


def test_han_only_handler_matches_original():
    ctx = PageContext(source_doc="AnNam", page=1, config=load_config(), mock_han_ocr=_detections())
    recs = layouts.get_handler("han_only").extract(ctx)
    assert [r.han for r in recs] == _orig_texts()


def test_three_block_preserves_characters():
    ctx = PageContext(source_doc="UcTraiTap", page=1, config=load_config(), mock_han_ocr=_detections())
    handler = layouts.route(ctx)
    assert handler.name == "three_block"
    recs = handler.extract(ctx)
    assert [r.han + r.phonetic + r.meaning for r in recs] == _orig_texts()
