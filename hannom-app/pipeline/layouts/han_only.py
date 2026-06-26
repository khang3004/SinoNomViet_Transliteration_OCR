"""``han_only`` layout — ported from the existing vertical-column engine.

(AGENTS.md §3.2.) A page of pure Han vertical columns, read right-to-left. This
is a thin wrapper over the vendored ``process_page_layout`` (the EXACT existing
AHT algorithm) — it does not change that algorithm's output. One record is
emitted per detected column, ``han`` = the column text in reading order.

Used as the FULL-OCR fallback when no text layer is present (e.g. the sample
image), so the app/worker still produces records end-to-end without a PDF.
"""

from __future__ import annotations

import os

from pipeline.layouts import register
from pipeline.layouts._spatial import detections_to_boxes, process_page_layout
from pipeline.layouts.base import filter_han_detections
from pipeline.page_context import PageContext
from pipeline.schema import Record, SourceOf


class HanOnlyHandler:
    """Pure vertical-Han page handler (full-OCR, RTL columns)."""

    name = "han_only"
    priority = 20  # fallback: checked after two_column and three_block

    def detect(self, page_ctx: PageContext) -> bool:
        # Applies to any page without a usable text layer — the generic
        # full-OCR fallback. (three_block, priority 10, gets first refusal.)
        return not page_ctx.has_text_layer()

    def extract(self, page_ctx: PageContext) -> list[Record]:
        dets = self._detections(page_ctx)
        dets = filter_han_detections(dets)
        if not dets:
            return []
        boxes = detections_to_boxes(dets)
        columns, _ordered = process_page_layout(boxes)

        image_name = self._image_name(page_ctx)
        records: list[Record] = []
        for line_no, col in enumerate(columns, start=1):
            han = col.full_text()
            records.append(
                Record(
                    id=self._record_id(page_ctx, line_no),
                    source_doc=page_ctx.source_doc,
                    page=page_ctx.page,
                    line_no=line_no,
                    han=han,
                    phonetic="",
                    meaning="",
                    han_chars=list(han),
                    layout_type=self.name,
                    image_path=image_name,
                    source_of=SourceOf(han="ocr"),
                )
            )
        return records

    # -- helpers --
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


register(HanOnlyHandler())
