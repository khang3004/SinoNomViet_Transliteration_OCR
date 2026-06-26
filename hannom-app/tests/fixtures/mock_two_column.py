"""MOCK fixtures for the two_column dry-run (AGENTS.md §1, §11.4).

The repo has NO real Châu bản PDF — only sample page images with no text layer.
So we MOCK the two inputs the hybrid pipeline needs:

  1. ``mock_text_spans()`` — synthetic PDF text-layer spans for the Vietnamese
     (right) column: two entries (#99, #100), each with labelled metadata lines,
     droppable headings (TRÍCH YẾU, "Công đồng …:"), and a parallel body line.
  2. ``mock_han_ocr()`` — synthetic Han OCR detections for the left column,
     positioned so each entry's Han pairs with its Vietnamese body by y-overlap,
     PLUS a watermark-like token ("LƯU TRỮ VN", low-conf, non-CJK) that the
     post-filter must drop.

These are synthetic/illustrative — only the few lines needed to prove the
pairing + metadata + watermark-filter logic, per AGENTS.md §1.
"""

from __future__ import annotations

from pipeline.pdf_text import TextSpan

PAGE_WIDTH = 600.0  # synthetic page width: Vietnamese column starts at x≈300 (right half)


def mock_text_spans() -> list[TextSpan]:
    """Synthetic Vietnamese text-layer spans (right column). x0≈300+, top→bottom."""
    s = TextSpan
    return [
        # --- Entry 99 -----------------------------------------------------
        s("99", 300, 40, 322, 58),  # entry-number token (left of body)
        s("TRÍCH YẾU", 345, 40, 430, 58),  # heading on the same line → dropped
        s("Công đồng truyền:", 345, 65, 480, 83),  # heading → dropped from body
        s("Ngày: 21 tháng 2 năm Gia Long 4", 345, 90, 560, 108),
        s("Tờ/Tập: 67/1", 345, 115, 440, 133),
        s("Loại: Truyền", 345, 140, 440, 158),
        s("Xuất xứ: Công Đồng", 345, 165, 500, 183),
        s("Đề tài: Báo cáo tình hình khai thác gỗ", 345, 190, 580, 208),
        s("Công đường quan doanh Bình Định được rõ: hỏi về số gỗ dân", 345, 215, 590, 233),
        # --- Entry 100 ----------------------------------------------------
        s("100", 300, 260, 330, 278),
        s("Công đồng truyền:", 345, 260, 480, 278),
        s("Ngày: 15 tháng 3 năm Gia Long 4", 345, 285, 560, 303),
        s("Tờ/Tập: 68/2", 345, 310, 440, 328),
        s("Loại: Tư", 345, 335, 420, 353),
        s("Báo cáo việc thu thuế tại các trấn", 345, 360, 560, 378),
    ]


def mock_han_ocr() -> list[dict]:
    """Synthetic Han OCR detections (left column) + one watermark token.

    Each detection is {text, bbox=[x0,y0,x1,y1], conf}. Han chars are stacked
    vertically inside each entry's y-band so y-overlap pairing assigns them
    correctly. The watermark token is low-confidence non-CJK ⇒ filtered out.
    """
    han = [
        # Entry 99 band (y≈40–233): 平定營公堂官
        ("平", 40, 55, 80, 85, 0.97),
        ("定", 40, 88, 80, 118, 0.96),
        ("營", 40, 121, 80, 151, 0.95),
        ("公", 40, 154, 80, 184, 0.95),
        ("堂", 40, 187, 80, 217, 0.94),
        ("官", 40, 205, 80, 232, 0.93),
        # Entry 100 band (y≈260–378): 諭旨
        ("諭", 40, 272, 80, 302, 0.96),
        ("旨", 40, 305, 80, 335, 0.95),
        # Watermark bleed — non-CJK, low confidence ⇒ dropped by post-filter.
        ("LƯU TRỮ VN", 120, 150, 230, 185, 0.15),
    ]
    return [
        {"text": t, "bbox": [x0, y0, x1, y1], "conf": c}
        for (t, x0, y0, x1, y1, c) in han
    ]
