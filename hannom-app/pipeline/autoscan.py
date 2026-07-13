"""Pure helpers for LLM full-page auto-scan (no DB, no network — unit-testable).

The vision model returns, per entry, bounding boxes **normalized 0–1000** in
Gemini's ``[ymin, xmin, ymax, xmax]`` convention plus the transcribed text, a
continuation flag and best-effort metadata. Here we convert those boxes to the
app's page-pixel space and shape them into record dicts (left ``pending``).
"""

from __future__ import annotations

import io

_META_KEYS = ("ngay", "to_tap", "loai", "xuat_xu", "de_tai")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_pixel_box(norm, width: int, height: int) -> list[float]:
    """``[ymin,xmin,ymax,xmax]`` (0–1000) → page-pixel ``[x0,y0,x1,y1]``.

    Returns ``[]`` if ``norm`` isn't four numbers. Coordinates are ordered
    (min/max) and clamped to the image so a box never falls outside the page.
    """
    if not isinstance(norm, (list, tuple)) or len(norm) != 4:
        return []
    ymin, xmin, ymax, xmax = (_num(v) for v in norm)
    if None in (ymin, xmin, ymax, xmax):
        return []
    x0 = min(xmin, xmax) / 1000.0 * width
    x1 = max(xmin, xmax) / 1000.0 * width
    y0 = min(ymin, ymax) / 1000.0 * height
    y1 = max(ymin, ymax) / 1000.0 * height
    x0, x1 = max(0.0, x0), min(float(width), x1)
    y0, y1 = max(0.0, y0), min(float(height), y1)
    if x1 <= x0 or y1 <= y0:
        return []
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def build_page_records(
    entries: list[dict], width: int, height: int, prefix: str, page: int,
    source_doc: str, image_path: str,
) -> tuple[list[dict], list[bool]]:
    """Turn parsed LLM entries into record dicts + a per-record continuation flag.

    Records get sequential ``line_no``/``entry_no`` (reading order), pixel boxes,
    ``entry_meta`` from ``meta``, and ``review_status="pending"`` (a human still
    verifies). Entries with no text and no box are skipped. The returned flag list
    is aligned 1:1 with the records list.
    """
    records: list[dict] = []
    flags: list[bool] = []
    line_no = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        han = (e.get("han") or "").strip()
        meaning = (e.get("vietnamese") or e.get("viet") or e.get("meaning") or "").strip()
        han_box = to_pixel_box(e.get("han_box"), width, height)
        viet_box = to_pixel_box(e.get("viet_box") or e.get("vi_box"), width, height)
        if not (han or meaning or han_box or viet_box):
            continue  # nothing usable — skip malformed entry
        line_no += 1
        meta_in = e.get("meta") or {}
        entry_meta = {k: str(meta_in.get(k, "") or "").strip() for k in _META_KEYS}
        records.append({
            "id": f"{prefix}.{page:03d}.{line_no:02d}",
            "source_doc": source_doc,
            "page": page,
            "line_no": line_no,
            "entry_no": line_no,
            "han": han,
            "han_raw": han,
            "han_conf": [],
            "phonetic": "",
            "meaning": meaning,
            "layout_type": "two_column",
            "image_path": image_path,
            "entry_meta": entry_meta,
            "han_chars": list(han),
            "phonetic_per_char": [],
            "source_of": {"han": "llm_autoscan", "phonetic": "", "meaning": "llm_autoscan"},
            "review_status": "pending",
            "han_bbox": han_box or None,
            "meaning_bbox": viet_box or None,
            "reviewed_by": None,
            "reviewed_at": None,
            "part_of": None,
        })
        flags.append(bool(e.get("is_continuation")))
    return records, flags


def downscale_for_llm(png_bytes: bytes, max_side: int = 2048) -> bytes:
    """Shrink a page PNG so the longest side <= ``max_side`` for a cheaper upload.

    Boxes are normalized (0–1000), so downscaling the image sent to the model does
    NOT affect coordinate mapping (the caller maps against the ORIGINAL page size).
    Returns the original bytes unchanged if already within bounds or on any error.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as im:
            if max(im.size) <= max_side:
                return png_bytes
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - never let a resize hiccup block the scan
        return png_bytes
