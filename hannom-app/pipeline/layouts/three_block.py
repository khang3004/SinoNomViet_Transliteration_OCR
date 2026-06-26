"""``three_block`` layout — Ức Trai Tập (SECONDARY / future).

(AGENTS.md §1, §3.2.) Ức Trai Tập pages stack three horizontal blocks per
column group: Han (top), phonetic transliteration (middle), Vietnamese meaning
(bottom). This is a SECONDARY doc type — a clean extension point, not a focus.

It is ported on top of the SAME vendored vertical-column engine
(``process_page_layout``) used by ``han_only``, so its column-grouping output is
identical to the existing logic; it then splits each column's boxes into the
three y-bands. The regression dry-run proves the column grouping matches the
original engine exactly (AGENTS.md §11.5).

TODO(uctrai): tune band boundaries / phonetic alignment against real Ức Trai Tập
pages when available. Current implementation is intentionally minimal.
"""

from __future__ import annotations

import os

from pipeline.layouts import register
from pipeline.layouts._spatial import detections_to_boxes, process_page_layout
from pipeline.page_context import PageContext
from pipeline.schema import Record, SourceOf


class ThreeBlockHandler:
    """Han / phonetic / meaning three-band handler (secondary)."""

    name = "three_block"
    priority = 10  # between two_column (0) and han_only (20)

    def detect(self, page_ctx: PageContext) -> bool:
        # Opt-in only: selected explicitly (source_doc hint) rather than by
        # heuristic, so it never steals pages from han_only/two_column. A real
        # detector keyed on the 3-band geometry can be added later.
        hint = (page_ctx.source_doc or "").lower()
        return "uctrai" in hint or "ức trai" in hint or "uc trai" in hint

    def extract(self, page_ctx: PageContext) -> list[Record]:
        dets = self._detections(page_ctx)
        if not dets:
            return []
        boxes = detections_to_boxes(dets)
        # Same column engine as the existing logic — output-identical grouping.
        columns, _ordered = process_page_layout(boxes)
        if not columns:
            return []

        image_name = self._image_name(page_ctx)
        records: list[Record] = []
        for line_no, col in enumerate(columns, start=1):
            han, phonetic, meaning = self._split_three_bands(col)
            records.append(
                Record(
                    id=self._record_id(page_ctx, line_no),
                    source_doc=page_ctx.source_doc,
                    page=page_ctx.page,
                    line_no=line_no,
                    han=han,
                    phonetic=phonetic,
                    meaning=meaning,
                    han_chars=list(han),
                    layout_type=self.name,
                    image_path=image_name,
                    source_of=SourceOf(
                        han="ocr",
                        phonetic="ocr" if phonetic else "",
                        meaning="ocr" if meaning else "",
                    ),
                )
            )
        return records

    @staticmethod
    def _split_three_bands(col) -> tuple[str, str, str]:
        """Split a column's boxes into top/middle/bottom thirds by y."""
        boxes = sorted(col.boxes, key=lambda b: b.cy)
        if not boxes:
            return "", "", ""
        ys = [b.cy for b in boxes]
        lo, hi = min(ys), max(ys)
        span = (hi - lo) or 1.0
        top, mid, bot = [], [], []
        for b in boxes:
            frac = (b.cy - lo) / span
            (top if frac < 1 / 3 else mid if frac < 2 / 3 else bot).append(b.text)
        return "".join(top), "".join(mid), "".join(bot)

    def _detections(self, page_ctx: PageContext) -> list[dict]:
        if page_ctx.mock_han_ocr is not None:
            return list(page_ctx.mock_han_ocr)
        if page_ctx.ocr_engine is None:
            return []
        return page_ctx.ocr_engine.ocr(page_ctx.image_path)

    def _record_id(self, page_ctx: PageContext, line_no: int) -> str:
        work = getattr(page_ctx.config, "work_id", "HVB_001") if page_ctx.config else "HVB_001"
        return f"{work}.{page_ctx.page:03d}.{line_no:02d}"

    @staticmethod
    def _image_name(page_ctx: PageContext) -> str:
        if page_ctx.image_path:
            return os.path.basename(page_ctx.image_path)
        return f"{page_ctx.source_doc}_p{page_ctx.page:04d}.png"


register(ThreeBlockHandler())
