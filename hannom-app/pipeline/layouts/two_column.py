"""PRIMARY layout: ``two_column`` — Châu bản "Mục lục" hybrid extraction.

(AGENTS.md §4.) Key fact: the Vietnamese (right) side is a REAL PDF text layer
(selectable, watermark-free); the Han (left) side is image-based.

Strategy:
  - Vietnamese  → from the PDF text layer (100% accurate, no OCR, no watermark).
  - Han         → OCR only the LEFT-column crop.
  - Pair Han-left ↔ Vietnamese-right per entry by **y-overlap**.

Extraction steps (mirrors AGENTS.md §4):
  1. detect: usable text layer AND Vietnamese spans cluster on the right while
     CJK/image content is on the left.
  2. Compute the column split x from the span x-distribution (not hardcoded).
  3. Group right-side spans into ENTRIES by leading entry numbers (98, 99, …)
     and y-bands.
  4. Pair Han-left ↔ Vietnamese-right per entry by y-overlap.
  5. Parse per-entry metadata from labelled lines (Ngày:/Tờ-Tập:/Loại:/Xuất xứ:/
     Đề tài:); drop headings TRÍCH YẾU / "Công đồng …:" from the parallel body.
  6. Han OCR post-filter: drop isolated low-confidence non-CJK tokens (watermark).
"""

from __future__ import annotations

import logging
import re

from pipeline.layouts import register
from pipeline.layouts.base import cjk_ratio, filter_han_detections
from pipeline.page_context import PageContext
from pipeline.pdf_text import TextSpan
from pipeline.schema import EntryMeta, Record, SourceOf

logger = logging.getLogger("hannom.layouts.two_column")

# Labels that introduce metadata lines, mapped to EntryMeta fields.
# Order matters: longer/more-specific labels first.
_META_LABELS: list[tuple[str, str]] = [
    ("Tờ/Tập:", "to_tap"),
    ("Tờ/ Tập:", "to_tap"),
    ("Xuất xứ:", "xuat_xu"),
    ("Đề tài:", "de_tai"),
    ("Ngày:", "ngay"),
    ("Loại:", "loai"),
]

# Headings to drop from the parallel (meaning) body.
_HEADING_PREFIXES = ("TRÍCH YẾU",)
_HEADING_REGEXES = (re.compile(r"^Công đồng\b.*:"),)  # "Công đồng truyền:" etc.

_ENTRY_NO_RE = re.compile(r"^(\d{1,4})\.?$")


class TwoColumnHandler:
    """Hybrid PDF-text + Han-OCR handler for Châu bản two-column pages."""

    name = "two_column"
    priority = 0  # checked FIRST by the router

    # ------------------------------------------------------------------
    def detect(self, page_ctx: PageContext) -> bool:
        """Step 1: usable text layer AND Vietnamese-right / image-left split.

        The Han (left) side is an IMAGE, so it contributes no text-layer spans:
        the selectable text is essentially all Vietnamese and sits in a column
        whose left edge is well to the right of the page origin. We therefore
        require (a) a usable text layer, (b) the text is Vietnamese (low CJK
        ratio), and (c) the Vietnamese column begins on the right portion of the
        page — confirmed via ``page_width`` when known, else inferred from the
        empty left band before the column's left edge.
        """
        if not page_ctx.has_text_layer():
            logger.debug("two_column.detect: no usable text layer.")
            return False
        spans = page_ctx.text_spans()
        if len(spans) < 5:
            return False
        all_text = " ".join(s.text for s in spans)
        if cjk_ratio(all_text) >= 0.2:
            logger.debug("two_column.detect: text layer is not Vietnamese.")
            return False
        left_edge = min(s.x0 for s in spans)
        if page_ctx.page_width:
            right_side = left_edge >= 0.30 * page_ctx.page_width
        else:
            # No width known: accept if there is a non-trivial empty band to the
            # left of the Vietnamese column (the Han image column lives there).
            right_side = left_edge > 0.0
        logger.debug(
            "two_column.detect: spans=%d left_edge=%.1f width=%s ⇒ %s",
            len(spans),
            left_edge,
            page_ctx.page_width,
            right_side,
        )
        return bool(right_side)

    # ------------------------------------------------------------------
    def extract(self, page_ctx: PageContext) -> list[Record]:
        spans = page_ctx.text_spans()
        split_x = self._column_split_x(spans)
        right_spans = [s for s in spans if s.cx >= split_x]

        # Step 3: group right-side spans into entries by entry numbers + y-bands.
        entries = self._group_entries(right_spans, split_x)
        if not entries:
            logger.warning("two_column.extract: no entries detected.")
            return []

        # Han side: OCR the left-column crop (mocked in dev/testing).
        han_dets = self._han_detections(page_ctx, split_x)
        # Step 6: drop watermark / noise tokens.
        han_dets = filter_han_detections(han_dets)

        image_name = self._image_name(page_ctx)
        records: list[Record] = []
        for line_no, entry in enumerate(entries, start=1):
            # Step 4: pair Han tokens by y-overlap with this entry's y-band.
            han_text = self._han_for_band(han_dets, entry["y0"], entry["y1"])
            # Step 5: parse metadata + build the parallel body (meaning).
            meta, meaning = self._parse_entry_body(entry["lines"])

            rec = Record(
                id=self._record_id(page_ctx, line_no),
                source_doc=page_ctx.source_doc,
                page=page_ctx.page,
                line_no=line_no,
                entry_no=entry["entry_no"],
                entry_meta=meta,
                image_path=image_name,
                han=han_text,
                phonetic="",  # this layout has no phonetic (AGENTS.md §5)
                meaning=meaning,
                han_chars=list(han_text),
                phonetic_per_char=[],
                layout_type=self.name,
                source_of=SourceOf(han="ocr", phonetic="", meaning="pdf_text"),
                review_status="pending",
            )
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _column_split_x(spans: list[TextSpan]) -> float:
        """Step 2: derive the Han|Vietnamese split x from the span distribution.

        The Vietnamese text-layer column has a well-defined LEFT EDGE
        (``min x0`` over all spans). The Han image column lies to the left of it;
        the gutter between them is just left of that edge. We place the split a
        median-glyph-width to the left of the Vietnamese column's left edge —
        data-driven, NOT a hardcoded pixel.
        """
        if not spans:
            return 0.0
        left_edge = min(s.x0 for s in spans)
        heights = sorted((s.y1 - s.y0) for s in spans if s.y1 > s.y0)
        margin = heights[len(heights) // 2] if heights else 10.0
        return max(left_edge - margin, 0.0)

    def _group_entries(self, right_spans: list[TextSpan], split_x: float) -> list[dict]:
        """Step 3: cluster right spans into lines, then split into entries.

        Returns a list of entries, each ``{entry_no, y0, y1, lines}`` where
        ``lines`` is a list of (line_text, y0, y1) tuples in reading order.
        """
        lines = self._cluster_lines(right_spans)
        if not lines:
            return []

        # An entry starts at a line whose left-most token is a bare entry number.
        entries: list[dict] = []
        current: dict | None = None
        for text, ly0, ly1, leftmost in lines:
            m = _ENTRY_NO_RE.match(leftmost.strip())
            starts_entry = m is not None
            if starts_entry:
                if current is not None:
                    entries.append(current)
                current = {
                    "entry_no": int(m.group(1)),
                    "y0": ly0,
                    "y1": ly1,
                    "lines": [],
                }
                # The remainder of the number line (after the number) is body text.
                remainder = text.strip()
                # strip the leading number token only
                remainder = re.sub(r"^\d{1,4}\.?\s*", "", remainder)
                if remainder:
                    current["lines"].append(remainder)
            elif current is not None:
                current["y1"] = ly1
                current["lines"].append(text.strip())
            # lines before the first entry number (page title etc.) are ignored.
        if current is not None:
            entries.append(current)
        return entries

    @staticmethod
    def _cluster_lines(spans: list[TextSpan]) -> list[tuple[str, float, float, str]]:
        """Group spans into text lines by y proximity.

        Returns ``(line_text, y0, y1, leftmost_token_text)`` sorted top→bottom.
        """
        if not spans:
            return []
        heights = sorted((s.y1 - s.y0) for s in spans if s.y1 > s.y0)
        med_h = heights[len(heights) // 2] if heights else 10.0
        tol = max(med_h * 0.6, 3.0)

        remaining = sorted(spans, key=lambda s: (s.cy, s.x0))
        lines: list[list[TextSpan]] = []
        for s in remaining:
            placed = False
            for line in lines:
                if abs(line[0].cy - s.cy) <= tol:
                    line.append(s)
                    placed = True
                    break
            if not placed:
                lines.append([s])

        out: list[tuple[str, float, float, str]] = []
        for line in lines:
            line.sort(key=lambda s: s.x0)
            text = " ".join(s.text for s in line)
            y0 = min(s.y0 for s in line)
            y1 = max(s.y1 for s in line)
            out.append((text, y0, y1, line[0].text))
        out.sort(key=lambda t: t[1])  # by y0
        return out

    def _parse_entry_body(self, lines: list[str]) -> tuple[EntryMeta, str]:
        """Step 5: split an entry's lines into metadata + parallel body.

        Metadata lines (Ngày:/Tờ-Tập:/…) populate EntryMeta. Heading lines
        (TRÍCH YẾU, "Công đồng …:") are dropped. Everything else is the meaning.
        """
        meta = EntryMeta()
        body_parts: list[str] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            label_field = self._match_meta_label(line)
            if label_field is not None:
                label, fld = label_field
                value = line[len(label):].strip()
                setattr(meta, fld, value)
                continue
            if self._is_heading(line):
                continue
            body_parts.append(line)
        meaning = " ".join(body_parts).strip()
        meaning = re.sub(r"\s{2,}", " ", meaning)
        return meta, meaning

    @staticmethod
    def _match_meta_label(line: str) -> tuple[str, str] | None:
        for label, fld in _META_LABELS:
            if line.startswith(label):
                return label, fld
        return None

    @staticmethod
    def _is_heading(line: str) -> bool:
        if any(line.startswith(p) for p in _HEADING_PREFIXES):
            return True
        return any(rx.match(line) for rx in _HEADING_REGEXES)

    # --- Han side ------------------------------------------------------
    def _han_detections(self, page_ctx: PageContext, split_x: float) -> list[dict]:
        """Get Han detections for the left column (PageContext owns rendering).

        For a real PDF, ``han_side_ocr`` renders the page at ``render_dpi`` and
        OCRs only the ``[0, split_x]`` crop; ``split_x`` and the text spans are
        in the SAME pixel space, so y-overlap pairing is consistent. For a plain
        image, it OCRs the whole page and we keep the Han (left) side here.

        TODO(real-pdf): validate end-to-end against a genuine text-layer Châu bản
        PDF when one is available (the render + Paddle path needs poppler + GPU).
        """
        dets = page_ctx.han_side_ocr(split_x)
        if page_ctx.pdf_path or page_ctx.mock_han_ocr is not None:
            return dets  # already restricted to the Han column / mock
        # Plain-image fallback: keep detections on the Han (left) side of split.
        return [
            d for d in dets
            if split_x <= 0 or (d["bbox"][0] + d["bbox"][2]) / 2.0 < split_x
        ]

    @staticmethod
    def _han_for_band(han_dets: list[dict], y0: float, y1: float) -> str:
        """Step 4: concatenate Han tokens whose centre-y falls in [y0, y1]."""
        in_band = [d for d in han_dets if y0 <= (d["bbox"][1] + d["bbox"][3]) / 2.0 <= y1]
        in_band.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))  # top→bottom
        return "".join(d["text"] for d in in_band)

    # --- ids / names ---------------------------------------------------
    def _record_id(self, page_ctx: PageContext, line_no: int) -> str:
        work = self._work_id(page_ctx)
        return f"{work}.{page_ctx.page:03d}.{line_no:02d}"

    @staticmethod
    def _work_id(page_ctx: PageContext) -> str:
        cfg = page_ctx.config
        return getattr(cfg, "work_id", "HVB_001") if cfg else "HVB_001"

    @staticmethod
    def _image_name(page_ctx: PageContext) -> str:
        import os

        if page_ctx.image_path:
            return os.path.basename(page_ctx.image_path)
        return f"{page_ctx.source_doc}_p{page_ctx.page:04d}.png"


register(TwoColumnHandler())
