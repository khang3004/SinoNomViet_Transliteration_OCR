"""Unit coverage for the Google Sheets export table-building (no DB / no network)."""

from __future__ import annotations

from pipeline.sheets import exporter


def _rows():
    # Two verified entries as returned by corpus_repo.export_entries (already merged
    # and ordered). The second carries full catalogue metadata.
    return [
        {"page": 1, "entry_no": 1, "han": "天下", "meaning": "thiên hạ",
         "ngay": "", "to_tap": "", "loai": "", "xuat_xu": "", "de_tai": "",
         "job_id": 2, "reviewer": "alice"},
        {"page": 20, "entry_no": 3, "han": "皇帝詔", "meaning": "chiếu của vua",
         "ngay": "Tự Đức 5", "to_tap": "Tờ 12", "loai": "Chiếu", "xuat_xu": "Nội các",
         "de_tai": "Bổ nhiệm", "job_id": 8, "reviewer": "bob"},
    ]


def test_headers():
    t = exporter.build_tables([])
    assert t["han_viet"] == [["Hán", "Việt"]]
    assert t["detail"][0] == [
        "Trang số", "Entry", "Hán", "Việt",
        "Ngày", "Tờ/Tập", "Loại", "Xuất xứ", "Đề tài", "Upload", "Người duyệt",
    ]


def test_han_viet_is_two_columns():
    t = exporter.build_tables(_rows())
    assert t["han_viet"] == [
        ["Hán", "Việt"],
        ["天下", "thiên hạ"],
        ["皇帝詔", "chiếu của vua"],
    ]


def test_detail_maps_metadata_to_the_right_cells():
    t = exporter.build_tables(_rows())
    # header + 2 rows
    assert len(t["detail"]) == 3
    assert t["detail"][2] == [
        "20", "3", "皇帝詔", "chiếu của vua",
        "Tự Đức 5", "Tờ 12", "Chiếu", "Nội các", "Bổ nhiệm", "8", "bob",
    ]


def test_none_values_become_empty_strings():
    rows = [{"page": None, "entry_no": None, "han": "字", "meaning": None,
             "ngay": None, "to_tap": None, "loai": None, "xuat_xu": None,
             "de_tai": None, "job_id": 5, "reviewer": None}]
    t = exporter.build_tables(rows)
    assert t["han_viet"][1] == ["字", ""]
    assert t["detail"][1] == ["", "", "字", "", "", "", "", "", "", "5", ""]
