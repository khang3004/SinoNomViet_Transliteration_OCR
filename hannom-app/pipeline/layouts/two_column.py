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

# Labels that introduce metadata lines (at line start), mapped to EntryMeta
# fields. Order matters: longer/more-specific labels first. Includes common OCR
# variants ("Để tài" for "Đề tài", "TờITập" for "Tờ/Tập", "Xuấtxứ"). ``Loại`` is
# handled separately (it appears inline after the entry number).
_META_LABELS: list[tuple[str, str]] = [
    ("Tờ/Tập:", "to_tap"),
    ("Tờ/ Tập:", "to_tap"),
    ("TờITập:", "to_tap"),
    ("Xuất xứ:", "xuat_xu"),
    ("Xuấtxứ:", "xuat_xu"),
    ("Đề tài:", "de_tai"),
    ("Để tài:", "de_tai"),
    ("Ngày:", "ngay"),
]

# Headings to drop from the parallel (meaning) body.
_HEADING_PREFIXES = ("TRÍCH YẾU",)
# A STANDALONE "Công đồng truyền:" sub-heading (nothing after the colon) is
# dropped; a "Công Đồng truyền: <content>" line is a body lead-in (kept).
_STANDALONE_HEADING_RE = re.compile(r"^Công\s*[đĐ]ồng\b[^:：]*[:：]\s*$", re.IGNORECASE)

# Body lead-in: the document-type that introduces the parallel body
# ("Chiếu:", "Chỉ Dụ:", "Công Đồng truyền: …", "Sử:", "Tư:", …). The meaning
# STARTS here (lead-in kept). Everything between the entry number and this line
# is metadata.
_BODY_LEADIN_RE = re.compile(
    r"^(Chiếu|Chỉ\s*[Dd]ụ|Dụ|Sử|Tư|Truyền|Sai|Tấu|Phụng|Phê|Tư\s*di|"
    r"Công\s*[đĐ]ồng\s+\S+)\s*[:：]",
    re.IGNORECASE,
)

# Watermark/noise filter for the Vietnamese side (Bug 4). A "noise" token is a
# short alphanumeric fragment mixing digits and letters ("2S", "3t", "2k") — the
# text-layer twin of Han glyphs / watermark bleed — or a configured watermark.
_NOISE_TOKEN_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-zÀ-ỹ])[\dA-Za-zÀ-ỹ]{1,3}$")

_ENTRY_NO_RE = re.compile(r"^(\d{1,4})\.?$")
# A line like "6 tháng 7 năm Gia Long 1" is a DATE, not an entry start.
_DATE_AFTER_NUM_RE = re.compile(r"^\d{1,4}\s+(tháng|năm|nhuận)\b", re.IGNORECASE)
# A standalone Vietnamese date line ("6 tháng 7 năm Gia Long 1") — on real Mục
# lục pages each entry BEGINS with one, so it is the reliable entry anchor.
_DATE_LINE_RE = re.compile(r"^\s*\d{1,3}\s+tháng\b.*\bnăm\b", re.IGNORECASE)
# The entry number sits right before "Loại:" — e.g. "; 11 Loại: Chiếu" → 11.
_NUM_LOAI_RE = re.compile(r"(\d{1,4})\s+Loại\s*[:：]", re.IGNORECASE)
# Value after "Loại:", up to the next label keyword or end of line.
_LOAI_VALUE_RE = re.compile(
    r"Loại\s*[:：]\s*(.+?)(?:\s+(?:Xuất|Xuấtxứ|Đề|Để|Tờ|TờI|Công|Ngày)\b|$)",
    re.IGNORECASE,
)

# Label/heading keywords unique to the Châu bản "Mục lục" format. Used by
# detect() as a fingerprint (tolerant of OCR typos like "Để tài" for "Đề tài").
_FINGERPRINT = (
    "TRÍCH YẾU",
    "Loại:",
    "Xuất xứ",
    "Đề tài",
    "Để tài",
    "Công Đồng truyền",
    "Công đồng truyền",
    "Tờ/Tập",
    "Ngày:",
)

# Minimum spans in the left cluster to treat it as a real (Han-garbage) column,
# and the maximum fraction of all spans it may hold (the Han column is always a
# minority — a larger left side means the gap is inside the Vietnamese column).
_MIN_LEFT_COL = 6
_MAX_LEFT_FRAC = 0.45
# Fallback Han|Vietnamese split as a fraction of page width when no clean gutter
# is found (the Han column occupies roughly the left third of a Mục lục page).
_LEFT_COL_FRAC = 0.35

# Symbols that almost never appear in real Vietnamese but riddle the mangled Han
# OCR tokens (e.g. "2E]Í#:", "X#3e&H])L"). A token containing any of these is
# treated as Han-OCR garbage and dropped from the Vietnamese side.
_GARBAGE_CHARS = frozenset("#{}[]*&%~^<>|\\@=+")


_VN_VOWELS = set("aàáảãạăắằẳẵặâấầẩẫậeèéẻẽẹêếềểễệiìíỉĩịoòóỏõọôốồổỗộơớờởỡợuùúủũụưứừửữựyỳýỷỹỵ")


def _is_garbage_token(text: str) -> bool:
    """True if a span looks like mangled Han OCR rather than Vietnamese."""
    t = text.strip()
    if not t:
        return True
    return any(c in _GARBAGE_CHARS for c in t)


def _is_vietnamese_word(text: str) -> bool:
    """True if a token is a real Vietnamese word (≥2 letters incl. a vowel).

    Used to locate the Vietnamese text column's left edge robustly, ignoring
    entry numbers and Han-twin noise that share the left region.
    """
    t = text.strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 2:
        return False
    return any(c.lower() in _VN_VOWELS for c in letters)


def _is_noise_token(text: str, watermark: "re.Pattern | None" = None) -> bool:
    """True if a Vietnamese-side token is watermark/Han-twin noise (Bug 4)."""
    t = text.strip()
    if not t:
        return True
    if watermark is not None and watermark.search(t):
        return True
    return bool(_NOISE_TOKEN_RE.match(t))


class TwoColumnHandler:
    """Hybrid PDF-text + Han-OCR handler for Châu bản two-column pages."""

    name = "two_column"
    priority = 0  # checked FIRST by the router

    # ------------------------------------------------------------------
    def detect(self, page_ctx: PageContext) -> bool:
        """Step 1: a Châu bản "Mục lục" text-layer page.

        Identified by its strong LABEL FINGERPRINT — the metadata labels and
        headings that are unique to this format (``TRÍCH YẾU``, ``Loại:``,
        ``Xuất xứ``, ``Đề tài``, ``Công Đồng truyền``, ``Tờ``, ``Ngày:``). This is
        far more robust than a pure-geometry test: on a real OCR'd PDF the Han
        column is mangled into Latin garbage that pollutes the left side and the
        x-distribution, but the Vietnamese labels are always present and reliable.
        """
        if not page_ctx.has_text_layer():
            logger.debug("two_column.detect: no usable text layer.")
            return False
        spans = page_ctx.text_spans()
        if len(spans) < 5:
            return False
        all_text = " ".join(s.text for s in spans)
        hits = sum(1 for kw in _FINGERPRINT if kw.lower() in all_text.lower())
        ok = hits >= 2
        logger.debug("two_column.detect: fingerprint hits=%d ⇒ %s", hits, ok)
        return ok

    # ------------------------------------------------------------------
    def extract(self, page_ctx: PageContext) -> list[Record]:
        spans = page_ctx.text_spans()
        split_x = self._column_split_x(spans, page_ctx.page_width)
        watermark = self._watermark_pattern()
        # Vietnamese column = right of the split, minus mangled-Han garbage tokens
        # and watermark/noise fragments ("2S", "3t") that bled into the text layer.
        right_spans = [
            s
            for s in spans
            if s.cx >= split_x
            and not _is_garbage_token(s.text)
            and not _is_noise_token(s.text, watermark)
        ]

        # Step 3: group right-side spans into entries by entry numbers + y-bands.
        entries = self._group_entries(right_spans, split_x)
        if not entries:
            logger.warning("two_column.extract: no entries detected.")
            return []

        # Bug 1 — Han crop boundary. The Han crop must stop BEFORE the metadata /
        # Vietnamese column: derive it from the text-layer span distribution (left
        # edge of the Vietnamese text minus a small margin), not a fixed ratio.
        page_w = page_ctx.page_width or (max((s.x1 for s in spans), default=split_x))
        han_crop_x = self._han_crop_x(spans, split_x)
        han_dets = self._han_detections(page_ctx, han_crop_x)
        # Step 6: drop watermark / noise tokens, then keep only CJK-majority
        # detections (removes any Latin label-bleed at the crop's right edge).
        han_dets = filter_han_detections(han_dets)
        han_dets = [d for d in han_dets if cjk_ratio(d.get("text", "")) >= 0.34]

        # Bug 4 — record the ACTUAL Vietnamese provenance and log the path used.
        used_text_layer = page_ctx.has_text_layer()
        meaning_src = "pdf_text" if used_text_layer else "ocr"
        logger.info(
            "two_column page %s: Vietnamese via %s; Han crop x≤%.0f (page_w=%.0f)",
            page_ctx.page,
            "text-layer" if used_text_layer else "OCR-fallback",
            han_crop_x,
            page_w,
        )

        image_name = self._image_name(page_ctx)
        records: list[Record] = []
        for line_no, entry in enumerate(entries, start=1):
            y0, y1 = entry["y0"], entry["y1"]
            # The Han block starts BELOW "TRÍCH YẾU" (skip the metadata block
            # above it). Fall back to the entry top if no heading was found.
            han_y0 = entry.get("han_y0") or y0
            # Step 4: pair Han tokens by y-overlap with the Han band (below
            # TRÍCH YẾU). If that band has no Han (e.g. an entry whose TRÍCH line
            # was detected low, or a layout without one), fall back to the full
            # entry band so we never lose the Han.
            han_text = self._han_for_band(han_dets, han_y0, y1)
            if not han_text and han_y0 > y0:
                han_text = self._han_for_band(han_dets, y0, y1)
                han_y0 = y0
            # Step 5: parse metadata + build the parallel body (meaning).
            meta, meaning = self._parse_entry_body(entry["lines"])
            # Block regions: Han box = below TRÍCH YẾU, left of the column split;
            # Vietnamese box spans the whole entry (it includes the metadata).
            han_bbox = [0.0, han_y0, han_crop_x, y1]
            meaning_bbox = [han_crop_x, y0, page_w, y1]

            rec = Record(
                id=self._record_id(page_ctx, line_no),
                source_doc=page_ctx.source_doc,
                page=page_ctx.page,
                line_no=line_no,
                entry_no=self._resolve_entry_no(entry),
                entry_meta=meta,
                image_path=image_name,
                han=han_text,
                han_raw=han_text,  # correction pass (runner) may overwrite han
                phonetic="",  # this layout has no phonetic (AGENTS.md §5)
                meaning=meaning,
                han_chars=list(han_text),
                phonetic_per_char=[],
                layout_type=self.name,
                source_of=SourceOf(han="ocr", phonetic="", meaning=meaning_src),
                review_status="pending",
                han_bbox=han_bbox,
                meaning_bbox=meaning_bbox,
            )
            records.append(rec)
        return records

    @staticmethod
    def _watermark_pattern() -> "re.Pattern | None":
        import os

        pat = os.environ.get("WATERMARK_PATTERN", "").strip()
        if not pat:
            return None
        try:
            return re.compile(pat, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid WATERMARK_PATTERN %r; ignoring.", pat)
            return None

    def _han_crop_x(self, spans: list[TextSpan], split_x: float) -> float:
        """Bug 1: tight Han|Vietnamese boundary from the text-layer distribution.

        Set it just left of the minimum x0 of Vietnamese TEXT spans (real words)
        — but only those RIGHT of the column split, so garbled Han-twin tokens in
        the low-x region don't pull the boundary into the Han image. This keeps
        the Han OCR crop clear of the metadata column. Warn if the boundary still
        overlaps any metadata span.
        """
        vn_x0s = [
            s.x0 for s in spans if _is_vietnamese_word(s.text) and s.x0 > split_x
        ]
        if not vn_x0s:
            return split_x
        heights = sorted((s.y1 - s.y0) for s in spans if s.y1 > s.y0)
        margin = heights[len(heights) // 2] if heights else 10.0
        crop_x = max(min(vn_x0s) - margin, 0.0)

        meta_x0s = [
            s.x0 for s in spans if self._match_meta_label(s.text.strip()) is not None
        ]
        if meta_x0s and crop_x > min(meta_x0s):
            logger.warning(
                "two_column: Han crop right edge %.0f overlaps metadata column "
                "(left=%.0f); metadata glyphs may leak into Han OCR.",
                crop_x,
                min(meta_x0s),
            )
        return crop_x

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _column_split_x(spans: list[TextSpan], page_width: float | None = None) -> float:
        """Step 2: derive the Han|Vietnamese split x from the span distribution.

        Two real cases:

        * **Clean text layer (Han is image-only):** all spans are Vietnamese; the
          split is just left of the column's left edge (``min x0`` − a glyph).

        * **OCR'd PDF (Han mangled into the text layer):** a dense LEFT cluster of
          garbage Han tokens sits to the left of the Vietnamese column. We find
          the widest gap in the sorted x0 distribution; if a substantial cluster
          (≥ ``_MIN_LEFT_COL`` spans) lies left of that gap, it's the Han column
          and the split is the gap midpoint. Otherwise we fall back to the clean
          case. Data-driven, NOT a hardcoded pixel.
        """
        if not spans:
            return 0.0
        x0s = sorted(s.x0 for s in spans)
        heights = sorted((s.y1 - s.y0) for s in spans if s.y1 > s.y0)
        margin = heights[len(heights) // 2] if heights else 10.0
        left_edge = x0s[0]
        clean_split = max(left_edge - margin, 0.0)

        # Find the gap separating the sparse left (Han-garbage) cluster from the
        # dense Vietnamese column. The Han column is always a MINORITY of the
        # text-layer spans, so we only accept a gap whose left side holds between
        # ``_MIN_LEFT_COL`` and ``_MAX_LEFT_FRAC`` of all spans — this prevents
        # the split from landing in the middle of the Vietnamese column (the
        # per-page instability that previously yielded empty Han crops). Among
        # qualifying gaps, take the widest (the true gutter).
        n = len(x0s)
        best_gap, best_mid = 0.0, None
        for i in range(1, n):
            a, b = x0s[i - 1], x0s[i]
            gap = b - a
            if gap <= 2.0 * margin:
                continue
            left_count = i  # spans strictly left of this gap
            if left_count < _MIN_LEFT_COL or left_count > _MAX_LEFT_FRAC * n:
                continue
            if gap > best_gap:
                best_gap, best_mid = gap, (a + b) / 2.0
        if best_mid is not None:
            return best_mid
        # No clean gutter found. On these uniformly-formatted Mục lục pages the
        # Han column occupies the left ~third, so fall back to a fraction of the
        # page width (the Han-garbage left edge alone, ``clean_split``, is useless
        # because ``min x0`` is itself garbage). Pure clean_split only when the
        # page width is unknown (idealized/mock pages with no left garbage).
        if page_width:
            return _LEFT_COL_FRAC * page_width
        return clean_split

    def _group_entries(self, right_spans: list[TextSpan], split_x: float) -> list[dict]:
        """Step 3: cluster right spans into lines, then split into entries.

        Returns a list of entries, each ``{entry_no, y0, y1, lines}`` where
        ``lines`` is a list of (line_text, y0, y1) tuples in reading order.
        """
        lines = self._cluster_lines(right_spans)
        if not lines:
            return []

        # Pick ONE anchor mode per page so the two formats don't fragment each
        # other:
        #  * date mode (real Mục lục): each entry BEGINS with a Vietnamese date
        #    line ("6 tháng 7 năm Gia Long 1"); the entry number sits inline
        #    before "Loại:". Used whenever the page has such date lines.
        #  * bare-number mode (idealized/mock): each entry begins with a bare
        #    number line. Used when there are no bare date lines.
        date_mode = any(_DATE_LINE_RE.match(t) for (t, *_rest) in lines)

        entries: list[dict] = []
        current: dict | None = None
        for text, ly0, ly1, leftmost in lines:
            t = text.strip()
            if date_mode:
                starts = bool(_DATE_LINE_RE.match(t))
                hint = None
            else:
                m_num = _ENTRY_NO_RE.match(leftmost.strip())
                starts = m_num is not None and not _DATE_AFTER_NUM_RE.match(t)
                hint = int(m_num.group(1)) if starts else None
            if starts:
                if current is not None:
                    entries.append(current)
                current = {"y0": ly0, "y1": ly1, "lines": [], "entry_no_hint": hint, "han_y0": None}
                if hint is not None:  # bare-number mode: drop the leading number
                    remainder = re.sub(r"^\d{1,4}\.?\s*", "", t)
                    if remainder:
                        current["lines"].append(remainder)
                else:  # date mode: keep the date line (→ ngay)
                    current["lines"].append(t)
            elif current is not None:
                current["y1"] = ly1
                current["lines"].append(t)
            # The Hán block begins just BELOW the "TRÍCH YẾU" heading; record its
            # bottom y so Han OCR skips the metadata block above it.
            if current is not None and t.startswith("TRÍCH"):
                current["han_y0"] = ly1
            # lines before the first entry anchor (page title) are ignored.
        if current is not None:
            entries.append(current)
        return entries

    @staticmethod
    def _resolve_entry_no(entry: dict) -> int | None:
        """Entry number ("Bài số"): the standalone integer just LEFT of "Loại:".

        On a real Mục lục line the entry number sits to the left of the ``Loại:``
        label, sometimes with OCR-bleed punctuation ("; 11 Loại: Chiếu",
        ": 21 Loại: …", "41 Loại: …") or a mangled glyph ("7n Loại:" → 7). It must
        NOT be confused with the Tờ/Tập FOLIO number, which can also share that
        line ("TờITập: 7/1 Loại: Sai" → that "1" is the folio, not the entry).

        Strategy: scan the entry's lines for the one containing ``Loại``; take the
        text BEFORE it. If a Tờ/Tập folio is present there, the leading number is
        the folio → no entry number (leave None for the reviewer). Otherwise the
        first digit-run before ``Loại`` is the entry number.
        """
        if entry.get("entry_no_hint") is not None:
            return entry["entry_no_hint"]
        for line in entry["lines"]:
            m = re.search(r"Lo[aạáàảãăắằ]i", line)  # tolerate OCR diacritic drift
            if not m:
                continue
            before = line[: m.start()]
            # A Tờ/Tập folio on this line ("TờITập: 7/1") owns the digits → skip.
            if re.search(r"Tờ|Tập|/\s*\d", before):
                return None
            nums = re.findall(r"\d{1,4}", before)
            return int(nums[0]) if nums else None
        # No "Loại:" line found — fall back to the old whole-entry scan.
        m = _NUM_LOAI_RE.search(" ".join(entry["lines"]))
        return int(m.group(1)) if m else None

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
        """Step 5: split an entry's lines into metadata + parallel body (Bug 2).

        Structure of a Châu bản entry:
            <entry no> <date> <Loại> <Xuất xứ> <Đề tài> [TRÍCH YẾU] <body lead-in>…

        Everything between the entry number and the document-type body lead-in
        ("Chiếu:", "Chỉ Dụ:", "Công Đồng truyền: …") is METADATA → ``entry_meta``.
        The body (``meaning``) STARTS at the lead-in and the lead-in is KEPT
        (consistent choice). Metadata may be labelled (Ngày:/Tờ-Tập:/Loại:/
        Xuất xứ:/Đề tài:) or POSITIONAL (e.g. "Đại Nội" = origin, "Tập hợp…" =
        topic) — both are removed from ``meaning``. Standalone headings
        (TRÍCH YẾU, bare "Công đồng truyền:") are dropped.
        """
        meta = EntryMeta()
        others: list[str] = []  # unlabelled, non-heading lines, in order
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if _DATE_LINE_RE.match(line):  # positional date → ngay
                if not meta.ngay:
                    meta.ngay = line
                continue
            if re.search(r"\bLoại\s*[:：]", line):  # inline/labelled Loại
                mv = _LOAI_VALUE_RE.search(line)
                if mv and not meta.loai:
                    meta.loai = mv.group(1).strip()
                continue
            label_field = self._match_meta_label(line)
            if label_field is not None:
                label, fld = label_field
                value = line[len(label):].strip()
                if not getattr(meta, fld):
                    setattr(meta, fld, value)
                continue
            if self._is_heading(line):  # TRÍCH YẾU / bare "Công đồng truyền:"
                continue
            others.append(line)

        # Find the body lead-in; everything before it is positional metadata.
        lead_idx = next(
            (i for i, ln in enumerate(others) if _BODY_LEADIN_RE.match(ln)), None
        )
        if lead_idx is not None:
            pre, body_parts = others[:lead_idx], others[lead_idx:]
            if pre and not meta.xuat_xu:
                meta.xuat_xu = pre[0]
            if len(pre) > 1 and not meta.de_tai:
                meta.de_tai = " ".join(pre[1:])
        else:
            body_parts = others  # no lead-in → all remaining lines are the body

        meaning = re.sub(r"\s{2,}", " ", " ".join(body_parts).strip())
        return meta, meaning

    @staticmethod
    def _match_meta_label(line: str) -> tuple[str, str] | None:
        for label, fld in _META_LABELS:
            if line.startswith(label):
                return label, fld
        return None

    @staticmethod
    def _is_heading(line: str) -> bool:
        """Standalone headings dropped from the body (TRÍCH YẾU, bare
        "Công đồng truyền:"). A "Công Đồng truyền: <content>" line is NOT a
        heading — it is the body lead-in (handled by _BODY_LEADIN_RE)."""
        if any(line.startswith(p) for p in _HEADING_PREFIXES):
            return True
        return bool(_STANDALONE_HEADING_RE.match(line))

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
        """Step 4: assemble the Han tokens in this entry's y-band in reading order.

        In this "Mục lục" the Hán summary is typeset HORIZONTALLY (a few lines
        read top-to-bottom, left-to-right within each line) — not vertical
        right-to-left woodblock columns. So order detections by row (y) then by
        column (x). (A vertical-column reader would mis-order this layout.)
        """
        in_band = [d for d in han_dets if y0 <= (d["bbox"][1] + d["bbox"][3]) / 2.0 <= y1]
        # Group into rows by y proximity, then read each row left-to-right.
        in_band.sort(key=lambda d: ((d["bbox"][1] + d["bbox"][3]) / 2.0, d["bbox"][0]))
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
