"""two_column dry-run (AGENTS.md §11.4) — NO GPU, NO Paddle, NO real PDF.

Feeds MOCK Vietnamese text spans (entry 99/100 + labelled metadata + headings)
and a MOCK Han OCR result (incl. a watermark token) through the real
``two_column`` handler, then prints the resulting records — proving:

  * correct han ↔ meaning pairing by y-overlap,
  * parsed ``entry_meta`` (Ngày/Tờ-Tập/Loại/Xuất xứ/Đề tài),
  * ``layout_type="two_column"`` and ``source_of.meaning="pdf_text"``,
  * the watermark token is filtered off the Han side.

Run:  python -m scripts.dryrun_two_column   (from hannom-app/)
"""

from __future__ import annotations

import json
import sys

try:  # ensure CJK prints on Windows consoles (cp1252/cp1258 default)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from pipeline import layouts
from pipeline.config import load_config
from pipeline.page_context import PageContext
from tests.fixtures.mock_two_column import PAGE_WIDTH, mock_han_ocr, mock_text_spans


def main() -> int:
    ctx = PageContext(
        source_doc="ChauBan",
        page=43,
        image_path="ChauBan_p0043.png",
        config=load_config(),
        page_width=PAGE_WIDTH,
        mock_text_spans=mock_text_spans(),
        mock_han_ocr=mock_han_ocr(),
    )

    print("=" * 72)
    print("two_column DRY-RUN (mock PDF text spans + mock Han OCR, no GPU)")
    print("=" * 72)

    handler = layouts.route(ctx)  # router must pick two_column first
    print(f"Router selected handler: {handler.name!r}")
    assert handler.name == "two_column", "router did not select two_column!"

    records = handler.extract(ctx)
    print(f"Extracted {len(records)} record(s).\n")

    for rec in records:
        print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
        print("-" * 72)

    # --- assertions that make the dry-run a real proof --------------------
    by_entry = {r.entry_no: r for r in records}
    assert 99 in by_entry and 100 in by_entry, "entries 99 and 100 expected"

    r99 = by_entry[99]
    assert r99.layout_type == "two_column"
    assert r99.source_of.meaning == "pdf_text", "meaning must be from pdf_text"
    assert r99.source_of.han == "ocr"
    assert r99.han == "平定營公堂官", f"unexpected Han pairing: {r99.han!r}"
    assert "LƯU TRỮ" not in r99.han, "watermark leaked into Han!"
    assert r99.entry_meta.ngay == "21 tháng 2 năm Gia Long 4"
    assert r99.entry_meta.to_tap == "67/1"
    assert r99.entry_meta.loai == "Truyền"
    assert r99.entry_meta.xuat_xu == "Công Đồng"
    assert r99.entry_meta.de_tai == "Báo cáo tình hình khai thác gỗ"
    assert "TRÍCH YẾU" not in r99.meaning, "TRÍCH YẾU heading not dropped"
    assert "Công đồng truyền" not in r99.meaning, "'Công đồng …:' heading not dropped"
    assert r99.meaning.startswith("Công đường quan doanh Bình Định")
    assert r99.phonetic == "", "two_column has no phonetic"

    r100 = by_entry[100]
    assert r100.han == "諭旨", f"unexpected Han pairing for #100: {r100.han!r}"

    print("\n✅ two_column dry-run PASSED:")
    print("   - han↔meaning paired by y (entry 99 → 平定營公堂官, entry 100 → 諭旨)")
    print("   - entry_meta parsed (Ngày/Tờ-Tập/Loại/Xuất xứ/Đề tài)")
    print("   - headings TRÍCH YẾU & 'Công đồng …:' dropped from meaning")
    print("   - watermark token 'LƯU TRỮ VN' filtered from Han side")
    print("   - layout_type=two_column, source_of.meaning=pdf_text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
