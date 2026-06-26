"""JSONL record schema (AGENTS.md §5).

One line = one paired Han/Vietnamese unit. The schema is **additive**: existing
fields stay valid; new fields default to null/empty so older readers keep working.

Three text fields (``han`` / ``phonetic`` / ``meaning``) are kept separate so
multiple training pairs can be exported from a single record.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceOf:
    """Provenance of each text field — where the value actually came from.

    ``meaning="pdf_text"`` marks text taken from the PDF text layer (highest
    trust, no OCR error). ``han="ocr"`` marks OCR-derived Han.
    """

    han: str = ""
    phonetic: str = ""
    meaning: str = ""


@dataclass
class EntryMeta:
    """Per-entry metadata parsed from the labelled Vietnamese lines.

    Fields map to the labels: ``Ngày:``, ``Tờ/Tập:``, ``Loại:``, ``Xuất xứ:``,
    ``Đề tài:``. All optional; absent labels stay empty.
    """

    ngay: str = ""
    to_tap: str = ""
    loai: str = ""
    xuat_xu: str = ""
    de_tai: str = ""

    def is_empty(self) -> bool:
        return not any((self.ngay, self.to_tap, self.loai, self.xuat_xu, self.de_tai))


@dataclass
class Record:
    """A single parallel Han/Vietnamese unit, serialisable to one JSONL line."""

    id: str
    source_doc: str
    page: int
    line_no: int
    han: str = ""
    phonetic: str = ""
    meaning: str = ""
    layout_type: str = ""
    image_path: str = ""
    entry_no: int | None = None
    entry_meta: EntryMeta = field(default_factory=EntryMeta)
    han_chars: list[str] = field(default_factory=list)
    phonetic_per_char: list[str] = field(default_factory=list)
    source_of: SourceOf = field(default_factory=SourceOf)
    review_status: str = "pending"
    # Block regions on the rendered page image, [x0, y0, x1, y1] in page-pixel
    # coords (at PDF_DPI). Used by the UI to overlay where each side came from.
    han_bbox: list[float] = field(default_factory=list)
    meaning_bbox: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Keep han_chars consistent with han unless caller set it explicitly.
        if self.han and not self.han_chars:
            self.han_chars = list(self.han)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict matching the JSONL schema field order."""
        d = asdict(self)
        # asdict turns nested dataclasses into dicts already; reorder for readability.
        return d

    def to_jsonl(self) -> str:
        """Serialise to a single JSON line (UTF-8, non-ASCII preserved)."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


def write_jsonl(records: list[Record], path: str) -> None:
    """Write records to ``path``, one JSON object per line (UTF-8)."""
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec.to_jsonl())
            fh.write("\n")
