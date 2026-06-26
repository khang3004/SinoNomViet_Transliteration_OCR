"""pytest coverage for the PRIMARY two_column handler (no GPU / no PDF)."""

from __future__ import annotations

from pipeline import layouts
from pipeline.config import load_config
from pipeline.page_context import PageContext
from tests.fixtures.mock_two_column import PAGE_WIDTH, mock_han_ocr, mock_text_spans


def _ctx() -> PageContext:
    return PageContext(
        source_doc="ChauBan",
        page=43,
        image_path="ChauBan_p0043.png",
        config=load_config(),
        page_width=PAGE_WIDTH,
        mock_text_spans=mock_text_spans(),
        mock_han_ocr=mock_han_ocr(),
    )


def test_router_selects_two_column_first():
    assert layouts.route(_ctx()).name == "two_column"


def test_two_entries_extracted():
    recs = layouts.get_handler("two_column").extract(_ctx())
    nums = sorted(r.entry_no for r in recs)
    assert nums == [99, 100]


def test_han_meaning_pairing_by_y():
    recs = {r.entry_no: r for r in layouts.get_handler("two_column").extract(_ctx())}
    assert recs[99].han == "平定營公堂官"
    assert recs[100].han == "諭旨"


def test_watermark_filtered_from_han():
    recs = layouts.get_handler("two_column").extract(_ctx())
    assert all("LƯU TRỮ" not in r.han for r in recs)


def test_entry_meta_parsed():
    recs = {r.entry_no: r for r in layouts.get_handler("two_column").extract(_ctx())}
    m = recs[99].entry_meta
    assert m.ngay == "21 tháng 2 năm Gia Long 4"
    assert m.to_tap == "67/1"
    assert m.loai == "Truyền"
    assert m.xuat_xu == "Công Đồng"
    assert m.de_tai == "Báo cáo tình hình khai thác gỗ"


def test_headings_dropped_and_provenance():
    recs = {r.entry_no: r for r in layouts.get_handler("two_column").extract(_ctx())}
    r99 = recs[99]
    assert "TRÍCH YẾU" not in r99.meaning
    assert "Công đồng truyền" not in r99.meaning
    assert r99.meaning.startswith("Công đường quan doanh Bình Định")
    assert r99.layout_type == "two_column"
    assert r99.source_of.meaning == "pdf_text"
    assert r99.source_of.han == "ocr"
    assert r99.phonetic == ""
