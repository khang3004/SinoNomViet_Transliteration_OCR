"""Reading the upstream team's ``ground_truth.jsonl`` / ``ground_truth.xlsx``.

The two files are complementary rather than redundant, which is why both are
needed and why they are merged rather than one being preferred:

* the **jsonl** carries the model outputs — ``gemini[]``, ``deepseek[]`` — plus
  ``ground_truth`` and ``label``, but no post identity beyond the filename;
* the **xlsx** carries the post identity — ``post_id``, ``caption``,
  ``post_link`` — plus ``ground_truth`` and ``gemini_ocr``.

They join on ``image``, the filename, which is the only field both share.

Records that are not pictures never enter the corpus. A row whose ``image`` is
blank, or does not name an image file, is dropped at ingest with a reason — a
reviewer should never be handed something they cannot look at.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from app.core.metrics import compare, normalize
from app.core.models import Band, CorpusRecord
from app.core.postid import parse_image_name, permalink_for, slug

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

# Column aliases: their headers have drifted between exports, and failing an
# ingest over a capitalised header would be a poor trade.
_COLUMNS = {
    "post_id": ("post_id", "postid", "post id", "id"),
    "image": ("image", "img", "filename", "file", "image_name"),
    "caption": ("caption", "fb caption", "fb_caption", "sub_caption"),
    "ground_truth": ("ground_truth", "groundtruth", "ground truth", "gt", "corrected"),
    "gemini": ("gemini_ocr", "gemini", "gemini ocr", "label"),
    "deepseek": ("deepseek_ocr", "deepseek", "deepseek ocr"),
    "post_link": ("post_link", "link", "post link", "url"),
}


def _image_key(value: Any) -> str:
    """Filename only, so ``images/x.jpg`` and ``x.jpg`` join."""
    if value is None:
        return ""
    text = str(value).strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def _is_image(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in IMAGE_SUFFIXES


def _text(value: Any) -> str:
    """Cell to string, with the spreadsheet null-isms removed.

    ``str(nan)`` is the string ``'nan'``, which silently becomes a three-character
    transcription that scores against the image. Guarded here once so no caller
    has to remember.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "#n/a"}:
        return ""
    return text


def _first_model_text(value: Any) -> str:
    """``[{"text": "..."}]`` -> the first non-empty text."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return _text(value.get("text"))
    if isinstance(value, list):
        for entry in value:
            text = _first_model_text(entry)
            if text:
                return text
    return ""


@dataclass
class IngestReport:
    """What ingest did, in enough detail to explain a surprising total."""

    jsonl_rows: int = 0
    xlsx_rows: int = 0
    records: int = 0
    matched: int = 0
    jsonl_only: int = 0
    xlsx_only: int = 0
    skipped: Counter = field(default_factory=Counter)
    bands: Counter = field(default_factory=Counter)

    def to_json(self) -> dict[str, Any]:
        return {
            "jsonl_rows": self.jsonl_rows,
            "xlsx_rows": self.xlsx_rows,
            "records": self.records,
            "matched": self.matched,
            "jsonl_only": self.jsonl_only,
            "xlsx_only": self.xlsx_only,
            "skipped": dict(self.skipped),
            "skipped_total": sum(self.skipped.values()),
            "bands": dict(self.bands),
        }


def classify(ground_truth: str, gemini: str) -> tuple[Band, dict[str, Any]]:
    """Which sampling band a record falls in, and the similarity behind it.

    Banding uses CER (reference = their ground truth) rather than the more
    generous max-length variant, so a record only reaches ``exact`` by actually
    being identical.
    """
    gt = normalize(ground_truth)
    gm = normalize(gemini)
    if not gt or not gm:
        return Band.EMPTY, compare(ground_truth, gemini).to_json()

    similarity = compare(ground_truth, gemini)
    if similarity.exact:
        band = Band.EXACT
    elif similarity.cer_accuracy >= 0.90:
        band = Band.NEAR
    elif similarity.cer_accuracy >= 0.50:
        band = Band.FAR
    else:
        band = Band.POOR
    return band, similarity.to_json()


def read_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    """Their ``ground_truth.jsonl``, keyed by image filename."""
    rows: dict[str, dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            key = _image_key(data.get("image"))
            if not key:
                continue
            rows[key] = {
                "image": key,
                "ground_truth": _text(data.get("ground_truth")),
                "label": _text(data.get("label")),
                "gemini": _first_model_text(data.get("gemini")),
                "deepseek": _first_model_text(data.get("deepseek")),
            }
    return rows


def _header_map(header: list[Any]) -> dict[str, int]:
    """Map our field names onto their column positions, however they spelled them."""
    seen = {str(name).strip().lower(): i for i, name in enumerate(header) if name}
    mapping: dict[str, int] = {}
    for field_name, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in seen:
                mapping[field_name] = seen[alias]
                break
    return mapping


def read_xlsx(path: Path) -> dict[str, dict[str, Any]]:
    """Their ``ground_truth.xlsx``, keyed by image filename."""
    try:
        import openpyxl
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is pinned
        raise RuntimeError("Missing dependency 'openpyxl' for .xlsx ingest.") from exc

    # read_only keeps a 9k-row sheet from being materialised as cell objects.
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book[book.sheetnames[0]]
        rows_iter: Iterator[tuple] = sheet.iter_rows(values_only=True)
        try:
            header = list(next(rows_iter))
        except StopIteration:
            return {}

        mapping = _header_map(header)
        if "image" not in mapping:
            raise ValueError(
                "ground_truth.xlsx has no 'image' column — found: "
                + ", ".join(str(h) for h in header if h)
            )

        def cell(row: tuple, name: str) -> str:
            index = mapping.get(name)
            if index is None or index >= len(row):
                return ""
            return _text(row[index])

        rows: dict[str, dict[str, Any]] = {}
        for row in rows_iter:
            if not row:
                continue
            key = _image_key(cell(row, "image"))
            if not key:
                continue
            rows[key] = {
                "image": key,
                "post_id": cell(row, "post_id"),
                "caption": cell(row, "caption"),
                "ground_truth": cell(row, "ground_truth"),
                "gemini": cell(row, "gemini"),
                "deepseek": cell(row, "deepseek"),
                "post_link": cell(row, "post_link"),
            }
        return rows
    finally:
        book.close()


def build(
    jsonl_path: Path | None = None,
    xlsx_path: Path | None = None,
) -> tuple[list[CorpusRecord], IngestReport]:
    """Merge whichever files are present into one record per image."""
    report = IngestReport()

    from_jsonl = read_jsonl(jsonl_path) if jsonl_path and jsonl_path.exists() else {}
    from_xlsx = read_xlsx(xlsx_path) if xlsx_path and xlsx_path.exists() else {}
    report.jsonl_rows = len(from_jsonl)
    report.xlsx_rows = len(from_xlsx)

    records: list[CorpusRecord] = []
    for key in sorted(set(from_jsonl) | set(from_xlsx)):
        left = from_jsonl.get(key, {})
        right = from_xlsx.get(key, {})

        if left and right:
            report.matched += 1
        elif left:
            report.jsonl_only += 1
        else:
            report.xlsx_only += 1

        if not _is_image(key):
            report.skipped["not_an_image_file"] += 1
            continue

        parsed = parse_image_name(key)
        post_id = right.get("post_id") or (parsed[0] if parsed else "")
        idx = parsed[1] if parsed else 0
        suffix = parsed[2] if parsed else Path(key).suffix.lower()
        if not post_id:
            report.skipped["no_post_id"] += 1
            continue

        # The jsonl is the authority on model output; the xlsx fills the gaps.
        ground_truth = left.get("ground_truth") or right.get("ground_truth", "")
        gemini = left.get("gemini") or right.get("gemini", "")
        deepseek = left.get("deepseek") or right.get("deepseek", "")

        if not ground_truth:
            # Nothing to audit: the whole task is judging their transcription.
            report.skipped["no_ground_truth"] += 1
            continue

        band, similarity = classify(ground_truth, gemini)
        report.bands[band.value] += 1

        sources = [name for name, present in (("jsonl", left), ("xlsx", right)) if present]
        records.append(
            CorpusRecord(
                record_id=f"{slug(post_id)}:{idx}",
                post_id=post_id,
                idx=idx,
                image=key,
                suffix=suffix,
                caption=right.get("caption", ""),
                ground_truth=ground_truth,
                label=left.get("label", ""),
                gemini=gemini,
                deepseek=deepseek,
                # Their link if they gave one; otherwise decode it ourselves.
                post_link=right.get("post_link") or permalink_for(post_id),
                band=band,
                gemini_similarity=similarity,
                sources=sources,
            )
        )

    report.records = len(records)
    log.info(
        "ingest: %d records from %d jsonl + %d xlsx rows (%d skipped)",
        report.records, report.jsonl_rows, report.xlsx_rows,
        sum(report.skipped.values()),
    )
    return records, report
