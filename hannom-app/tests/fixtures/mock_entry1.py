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
    rows = [
        # row 1 (y≈120)
        ("詔", 40, 110, 70, 140), ("阮", 75, 110, 105, 140), ("福", 110, 110, 140, 140),
        ("乃", 145, 110, 175, 140), ("威", 180, 110, 210, 140), ("侯", 215, 110, 245, 140),
        # row 2 (y≈160) — ends with 拜詣
        ("裔", 40, 156, 70, 186), ("宗", 75, 156, 105, 186), ("泰", 110, 156, 140, 186),
        ("招", 145, 156, 175, 186), ("拜", 180, 156, 210, 186), ("詣", 215, 156, 245, 186),
    ]
    return [{"text": t, "bbox": [x0, y0, x1, y1], "conf": 0.9} for (t, x0, y0, x1, y1) in rows]
