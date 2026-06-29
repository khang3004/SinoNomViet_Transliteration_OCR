"""Realistic MOCK of Châu bản page-0 entry 1 (for the quality-fix tests).

Reproduces the real structure (Bug-fix verification): a Vietnamese text-layer
column on the right (entry no + date + Loại + positional Xuất xứ "Đại Nội" +
positional Đề tài + Tờ/Tập + TRÍCH YẾU + body lead-in "Chiếu: …"), a Han image
column on the left (incl. gold "拜詣"), and a watermark/noise token "2S" bled
into the Vietnamese body. Coordinates use a 600-px-wide synthetic page; the
Vietnamese column starts at x0=300, the Han image is left of ~250.
"""

from __future__ import annotations

from pipeline.pdf_text import TextSpan

PAGE_WIDTH = 600.0


def entry1_text_spans() -> list[TextSpan]:
    s = TextSpan
    return [
        s("6 tháng 7 năm Gia Long 1", 300, 20, 520, 38),       # date → ngay
        s("11", 300, 44, 318, 62),                              # entry number
        s("Loại: Chiếu", 325, 44, 430, 62),                     # loai
        s("Đại Nội", 300, 68, 360, 86),                         # Xuất xứ (positional)
        s("Tập hợp con cháu trong họ,", 300, 92, 520, 110),     # Đề tài (positional)
        s("Tờ/Tập: 1/1", 300, 116, 400, 134),                  # to_tap (labelled)
        s("TRÍCH YẾU", 300, 140, 400, 158),                     # heading → dropped
        s("Chiếu: Nguyễn Phúc Phấn, là", 300, 164, 560, 182),   # body lead-in
        s("con cháu Uy Thọ hầu Nguyễn Tôn", 300, 188, 560, 206),
        s("2S", 565, 188, 585, 206),                            # watermark/noise bleed
        s("Thái, hãy chiêu tập họ tộc để về thành", 300, 212, 580, 230),
        s("Thăng Long bái yết.", 300, 236, 460, 254),
    ]


def entry1_han_ocr() -> list[dict]:
    """Han image OCR (left column), two horizontal rows incl. gold 拜詣."""
    # Hán sits BELOW the "TRÍCH YẾU" heading (y 140–158), as on a real page.
    rows = [
        # row 1 (y≈180)
        ("詔", 40, 165, 70, 195), ("阮", 75, 165, 105, 195), ("福", 110, 165, 140, 195),
        ("乃", 145, 165, 175, 195), ("威", 180, 165, 210, 195), ("侯", 215, 165, 245, 195),
        # row 2 (y≈215) — ends with 拜詣
        ("裔", 40, 200, 70, 230), ("宗", 75, 200, 105, 230), ("泰", 110, 200, 140, 230),
        ("招", 145, 200, 175, 230), ("拜", 180, 200, 210, 230), ("詣", 215, 200, 245, 230),
    ]
    return [{"text": t, "bbox": [x0, y0, x1, y1], "conf": 0.9} for (t, x0, y0, x1, y1) in rows]
