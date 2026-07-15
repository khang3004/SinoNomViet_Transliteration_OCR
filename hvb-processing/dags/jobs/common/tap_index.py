from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


_TAP_LABEL_RE = re.compile(
    r"(?P<trieu>GIA\s*LONG|MINH\s*M[ẸE]NH|THI[ỆE]U\s*TR[ỊI]|T[ỰU]\s*[ĐD][ỨU]C|"
    r"KI[ẾE]N\s*PH[ÚU]C|H[ÀA]M\s*NGHI|[ĐD][ỒO]NG\s*KH[ÁA]NH|TH[ÀA]NH\s*TH[ÁA]I|"
    r"DUY\s*T[ÂA]N|KH[ẢA]I\s*[ĐD][ỊI]NH|B[ẢA]O\s*[ĐD][ẠA]I)"
    r"\s*T[ẬA]P\s*(?P<so>\d+)",
    re.IGNORECASE,
)


def _slug(text: str) -> str:
    # ASCII slug for tap_id / Slug ASCII cho tap_id
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", stripped.lower()).strip("_")


def parse_tap_label(label: str) -> dict[str, Any] | None:
    """Parse 'MINH MỆNH TẬP 7' into trieu/tap_so/tap_id.

    Parse nhãn tập thành triều / số tập / tap_id.
    """
    match = _TAP_LABEL_RE.search(str(label or ""))
    if not match:
        return None
    trieu = re.sub(r"\s+", " ", match.group("trieu")).strip().upper()
    tap_so = int(match.group("so"))
    tap_id = f"{_slug(trieu)}_tap_{tap_so:02d}"
    return {
        "trieu_dai": trieu,
        "tap_so": tap_so,
        "tap_id": tap_id,
        "label": f"{trieu} TẬP {tap_so}",
    }


@lru_cache(maxsize=4)
def load_tap_index(doc_id: str = "hvb_base") -> dict[str, Any]:
    """Load shipped tap index JSON for a doc.

    Load file tap_index đi kèm cho một doc.
    """
    path = Path(__file__).resolve().parent / "data" / f"{doc_id}_tap_index.json"
    if not path.exists():
        return {"doc_id": doc_id, "parents": [], "blank_pages": [], "batches_by_tap": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"doc_id": doc_id, "parents": [], "blank_pages": []}


def blank_page_set(doc_id: str = "hvb_base") -> set[int]:
    # Pure blank PDF pages to skip OCR / Trang PDF trắng — bỏ OCR
    return {int(x) for x in (load_tap_index(doc_id).get("blank_pages") or [])}


def parent_page_map(doc_id: str = "hvb_base") -> dict[int, dict[str, Any]]:
    # Map parent_page_no -> tap metadata / Map trang parent -> metadata tập
    out: dict[int, dict[str, Any]] = {}
    for row in load_tap_index(doc_id).get("parents") or []:
        if not isinstance(row, dict) or row.get("page_no") is None:
            continue
        page_no = int(row["page_no"])
        parsed = parse_tap_label(str(row.get("label") or ""))
        meta = {
            "trieu_dai": row.get("trieu_dai") or (parsed or {}).get("trieu_dai"),
            "tap_so": int(row.get("tap_so") or (parsed or {}).get("tap_so") or 0),
            "tap_id": row.get("tap_id") or (parsed or {}).get("tap_id"),
            "label": row.get("label") or (parsed or {}).get("label"),
            "parent_page_no": page_no,
        }
        if meta["tap_id"]:
            out[page_no] = meta
    return out


def active_tap_for_page(doc_id: str, page_no: int) -> dict[str, Any] | None:
    """Return tap covering page_no (latest parent_page_no <= page_no).

    Tập đang áp dụng cho page_no (parent gần nhất bên trái).
    """
    parents = parent_page_map(doc_id)
    if not parents:
        return None
    chosen: dict[str, Any] | None = None
    for parent_page in sorted(parents):
        if parent_page <= page_no:
            chosen = parents[parent_page]
        else:
            break
    return chosen


def is_tap_parent_page(doc_id: str, page_no: int) -> bool:
    return page_no in parent_page_map(doc_id)


def is_blank_page(doc_id: str, page_no: int) -> bool:
    return page_no in blank_page_set(doc_id)


def tap_payload_for_page(doc_id: str, page_no: int) -> dict[str, Any] | None:
    # Tap metadata to stamp on OCR/entry JSON / Metadata tập gắn vào OCR/entry
    return active_tap_for_page(doc_id, page_no)


def batches_by_tap(doc_id: str = "hvb_base") -> list[dict[str, Any]]:
    return list(load_tap_index(doc_id).get("batches_by_tap") or [])
