from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.chau_ban_schema import PIPELINE_VERSION, extract_json_object, make_entry_pair_id, utc_now_iso
from common.config import get_value, load_config
from common.entry_quality import (
    CONFIDENCE_METHOD,
    clean_trich_yeu,
    derive_entry_status,
    detect_entry_flags,
    estimate_entry_ocr_confidence,
    should_refine_entry,
)
from common.io_storage import list_objects_with_prefix, upload_json_payload, download_object, entry_object_key
from common.llm_chat import call_chat_completion
from common.page_utils import parse_pages_filter

_REFINE_PROMPT = """Bạn là chuyên gia hiệu đính OCR mục lục Châu bản triều Nguyễn.
Cho một entry JSON (chỉ mục + trích yếu Hán/Việt). Hãy hiệu đính NHẸ:

- Sửa lỗi rõ (None/null rác, khoảng trắng, nhãn lệch).
- Giữ \\n xuống dòng nếu hợp lý; không gộp thành một đoạn nếu nguồn có nhiều dòng.
- KHÔNG bịa nội dung không có trong nguồn.
- KHÔNG dịch lại / viết lại cho hay hơn.
- Nếu text bị cắt giữa chừng: giữ nguyên phần có; đặt truncated=true.

Trả về ĐÚNG một JSON (không markdown):
{{
  "chi_muc": {{
    "ngay_thang": "...|null",
    "to_tap": "...|null",
    "the_loai": "...|null",
    "xuat_xu": "...|null",
    "de_tai": "...|null"
  }},
  "trich_yeu": {{"han_nom": "...", "quoc_ngu": "..."}},
  "refine_confidence": 0.0,
  "truncated": false,
  "notes": "ngắn"
}}

ENTRY JSON:
{entry_json}
"""


def _load_json(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_refine" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {bucket}/{object_key}")
    return data


def _apply_refine_result(entry: dict[str, Any], refined: dict[str, Any], model: str) -> dict[str, Any]:
    # Merge refine model output into entry / Ghép output refine vào entry
    chi_muc = refined.get("chi_muc") if isinstance(refined.get("chi_muc"), dict) else entry.get("chi_muc")
    trich = clean_trich_yeu(refined.get("trich_yeu") or entry.get("trich_yeu"))
    is_last = False
    sources = entry.get("source_pages") or []
    if sources and isinstance(sources[0], dict):
        is_last = bool(sources[0].get("is_last_on_page"))
    flags = detect_entry_flags(chi_muc=chi_muc or {}, trich_yeu=trich, is_last_on_page=is_last)
    if refined.get("truncated"):
        flags = sorted(set(flags + ["truncated_reported"]))
    try:
        refine_confidence = float(refined.get("refine_confidence"))
    except (TypeError, ValueError):
        refine_confidence = 0.0
    heuristic = estimate_entry_ocr_confidence(chi_muc=chi_muc or {}, trich_yeu=trich, flags=flags)
    if refine_confidence <= 0:
        refine_confidence = heuristic
    refine_confidence = max(refine_confidence, heuristic)
    # Keep partial text indexable / Giữ text một phần vẫn index được
    if trich["han_nom"] or trich["quoc_ngu"]:
        refine_confidence = max(refine_confidence, 0.5)

    entry_id = str(entry.get("entry_id") or "")
    content_alignment: list[dict[str, Any]] = []
    if trich["han_nom"] or trich["quoc_ngu"]:
        content_alignment.append(
            {
                "pair_index": 0,
                "pair_id": make_entry_pair_id(entry_id, 0) if entry_id else entry.get("entry_id"),
                "han_nom": trich["han_nom"],
                "quoc_ngu": trich["quoc_ngu"],
                "source_page": (sources[0].get("page_no") if sources else None),
                "source_kind": "refined_trich_yeu",
            }
        )

    entry.update(
        {
            "chi_muc": chi_muc,
            "trich_yeu": trich,
            "content_alignment": content_alignment,
            "text_views": {"han_nom_full": trich["han_nom"], "quoc_ngu_full": trich["quoc_ngu"]},
            "flags": flags,
            "refine_confidence": round(min(0.99, max(0.0, refine_confidence)), 4),
            "confidence_method": CONFIDENCE_METHOD,
            "refine_model": model,
            "refine_at": utc_now_iso(),
            "refine_notes": refined.get("notes"),
            "status": derive_entry_status(flags, refined=True),
            "pipeline_version": PIPELINE_VERSION,
            "updated_at": utc_now_iso(),
        }
    )
    return entry


def refine_catalog_entries(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    force_all: bool = False,
    upload_minio: bool = True,
) -> dict[str, int]:
    """DeepSeek refine for low-confidence / flagged STT entries.

    Hiệu đính DeepSeek cho entry STT confidence thấp / có cờ lỗi.
    """
    cfg = load_config()
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    threshold = float(get_value(cfg, "pipeline", "refine_confidence_threshold", fallback="0.7"))
    page_filter = parse_pages_filter(pages)

    keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("stt_")
    ]

    totals = {"scanned": 0, "refined": 0, "skipped": 0}
    for key in keys:
        entry = _load_json(bucket_entries, key)
        totals["scanned"] += 1
        sources = entry.get("source_pages") or []
        if page_filter is not None:
            page_nos = {
                int(row.get("page_no"))
                for row in sources
                if isinstance(row, dict) and row.get("page_no") is not None
            }
            if page_nos and page_nos.isdisjoint(page_filter):
                totals["skipped"] += 1
                continue

        ocr_conf = float(entry.get("ocr_confidence") or 0.0)
        flags = list(entry.get("flags") or [])
        if not force_all and not should_refine_entry(ocr_conf, flags, threshold=threshold):
            totals["skipped"] += 1
            continue

        compact = {
            "stt": entry.get("stt"),
            "chi_muc": entry.get("chi_muc"),
            "trich_yeu": entry.get("trich_yeu"),
            "flags": flags,
            "ocr_confidence": ocr_conf,
        }
        prompt = _REFINE_PROMPT.format(entry_json=json.dumps(compact, ensure_ascii=False))
        raw, model = call_chat_completion(prompt)
        parsed = extract_json_object(raw) or {}
        updated = _apply_refine_result(entry, parsed, model)
        if upload_minio:
            uri = upload_json_payload(bucket_entries, entry_object_key(doc_id, updated), updated)
            print(
                f"[refine] {uri} conf={updated.get('refine_confidence')} "
                f"flags={updated.get('flags')} status={updated.get('status')}"
            )
        totals["refined"] += 1

    print(f"[refine] done: {totals}")
    return totals
