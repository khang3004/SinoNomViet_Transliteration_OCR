"""Quality-fix tests for two_column (Bugs 1–4) on a realistic entry-1 mock."""

from __future__ import annotations

import pytest

from pipeline import layouts
from pipeline.config import Config
from pipeline.correct import get_corrector, register
from pipeline.layouts.two_column import TwoColumnHandler
from pipeline.page_context import PageContext
from pipeline.runner import _apply_correction
from pipeline.schema import Record
from tests.fixtures.mock_entry1 import PAGE_WIDTH, entry1_han_ocr, entry1_text_spans


def _ctx() -> PageContext:
    return PageContext(
        source_doc="ChauBan",
        page=1,
        image_path="ChauBan_p0001.png",
        config=Config(correct_backend="skip", translate_backend="skip"),
        page_width=PAGE_WIDTH,
        mock_text_spans=entry1_text_spans(),
        mock_han_ocr=entry1_han_ocr(),
    )


def _record() -> Record:
    recs = layouts.get_handler("two_column").extract(_ctx())
    assert len(recs) == 1
    return recs[0]


# --- Bug 1: Han crop boundary stops before the metadata column ------------
def test_bug1_han_crop_excludes_metadata():
    spans = entry1_text_spans()
    h = TwoColumnHandler()
    crop_x = h._han_crop_x(spans, split_x=0.0)
    # left edge of the metadata / Vietnamese column is x0=300
    meta_left = min(s.x0 for s in spans if h._match_meta_label(s.text.strip()))
    assert crop_x < meta_left, f"crop {crop_x} overlaps metadata at {meta_left}"
    # and it must not cut the Han (max Han x1 = 245)
    assert crop_x >= 245
    # the record's han block bbox right edge equals the tight crop
    rec = _record()
    assert rec.han_bbox[2] == pytest.approx(crop_x)
    assert rec.han_bbox[2] < meta_left


# --- Bug 2: metadata fully parsed, none leaks into meaning ----------------
def test_bug2_metadata_not_in_meaning():
    rec = _record()
    m = rec.entry_meta
    assert m.ngay == "6 tháng 7 năm Gia Long 1"
    assert m.to_tap == "1/1"
    assert m.loai == "Chiếu"
    assert m.xuat_xu == "Đại Nội"           # positional
    assert m.de_tai == "Tập hợp con cháu trong họ,"  # positional
    # meaning starts at the document-type lead-in and excludes ALL metadata
    assert rec.meaning.startswith("Chiếu:")
    for leaked in ("Đại Nội", "Tập hợp con cháu", "TRÍCH YẾU", "Tờ/Tập", "Loại:"):
        assert leaked not in rec.meaning, f"metadata {leaked!r} leaked into meaning"
    assert rec.meaning.endswith("Thăng Long bái yết.")
    assert rec.entry_no == 11


# --- Bug 3: correction is opt-in (skip ⇒ han==raw); stub proves the path ---
def test_bug3_skip_keeps_raw():
    rec = _record()
    assert rec.han == rec.han_raw
    assert "拜詣" in rec.han  # gold preserved


def test_bug3_correction_stub_runs_and_keeps_raw():
    class _Stub:
        name = "stub_fix"

        def __init__(self, config=None):
            pass

        def correct(self, han: str) -> str:
            return han.replace("調", "詣")  # repair a known OCR confusion

    register("stub_fix", _Stub)
    recs = [Record(id="x", source_doc="d", page=1, line_no=1, han="拜調", han_raw="拜調")]
    _apply_correction(recs, Config(correct_backend="stub_fix"))
    assert recs[0].han == "拜詣"      # corrected
    assert recs[0].han_raw == "拜調"  # raw preserved


def test_bug3_dict_corrector_loads():
    # The dict corrector must instantiate even if dicts are absent (no-op then).
    c = get_corrector(Config(correct_backend="dict"))
    assert c.correct("拜詣") == "拜詣"


# --- Bug 4: watermark filtered; provenance accurate -----------------------
def test_bug4_watermark_filtered_and_source():
    rec = _record()
    assert "2S" not in rec.meaning, "watermark/noise leaked into Vietnamese"
    assert rec.source_of.meaning == "pdf_text"  # came from the text layer


def test_bug4_router_selects_two_column():
    assert layouts.route(_ctx()).name == "two_column"
