"""Unit coverage for AI auto-scan box conversion + record building (no DB/network)."""

from __future__ import annotations

from pipeline import autoscan


# --- to_pixel_box ---------------------------------------------------------
def test_to_pixel_box_scales_and_orders():
    # [ymin,xmin,ymax,xmax] normalized 0-1000 on a 1000x2000 page.
    assert autoscan.to_pixel_box([0, 0, 500, 250], 1000, 2000) == [0.0, 0.0, 250.0, 1000.0]


def test_to_pixel_box_clamps_out_of_range():
    box = autoscan.to_pixel_box([-100, 900, 1100, 1200], 1000, 1000)
    assert box == [900.0, 0.0, 1000.0, 1000.0]  # clamped into the page


def test_to_pixel_box_rejects_malformed():
    assert autoscan.to_pixel_box([1, 2, 3], 100, 100) == []
    assert autoscan.to_pixel_box("nope", 100, 100) == []
    assert autoscan.to_pixel_box([0, 0, 0, 0], 100, 100) == []  # zero-area


# --- build_page_records ---------------------------------------------------
def _entries():
    return [
        {"han_box": [0, 500, 100, 900], "viet_box": [0, 0, 100, 500],
         "han": "天下", "vietnamese": "thiên hạ", "is_continuation": False,
         "meta": {"ngay": "Tự Đức 5", "loai": "Chiếu"}},
        {"han_box": [100, 500, 200, 900], "viet_box": [100, 0, 200, 500],
         "han": "續文", "vietnamese": "tiếp theo", "is_continuation": True},
    ]


def test_build_page_records_ids_status_and_flags():
    recs, flags = autoscan.build_page_records(_entries(), 1000, 1000, "HVB_001", 7, "ChauBan", "p0007.png")
    assert [r["id"] for r in recs] == ["HVB_001.007.01", "HVB_001.007.02"]
    assert [r["line_no"] for r in recs] == [1, 2]
    assert [r["entry_no"] for r in recs] == [1, 2]
    assert all(r["review_status"] == "pending" for r in recs)  # never auto-verified
    assert flags == [False, True]
    assert recs[0]["image_path"] == "p0007.png" and recs[0]["page"] == 7


def test_build_page_records_boxes_and_metadata():
    recs, _ = autoscan.build_page_records(_entries(), 1000, 1000, "HVB_001", 7, "ChauBan", "p.png")
    assert recs[0]["han_bbox"] == [500.0, 0.0, 900.0, 100.0]
    assert recs[0]["meaning_bbox"] == [0.0, 0.0, 500.0, 100.0]
    assert recs[0]["entry_meta"] == {"ngay": "Tự Đức 5", "to_tap": "", "loai": "Chiếu",
                                     "xuat_xu": "", "de_tai": ""}
    assert recs[0]["han_chars"] == ["天", "下"]
    assert recs[0]["source_of"]["han"] == "llm_autoscan"


def test_build_page_records_skips_empty_entries():
    entries = [{"han": "", "vietnamese": "", "han_box": None, "viet_box": None},
               {"han": "有", "vietnamese": "", "han_box": [0, 0, 10, 10]}]
    recs, flags = autoscan.build_page_records(entries, 100, 100, "HVB_001", 1, "d", "p.png")
    assert len(recs) == 1 and recs[0]["id"] == "HVB_001.001.01" and len(flags) == 1
