from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.chau_ban_schema import PIPELINE_VERSION, make_entry_id, make_entry_pair_id, utc_now_iso
from common.config import get_value, load_config
from common.entry_quality import (
    CONFIDENCE_METHOD,
    clean_trich_yeu,
    derive_entry_status,
    detect_entry_flags,
    estimate_entry_ocr_confidence,
)
from common.io_storage import (
    download_object,
    entry_object_key,
    list_objects_with_prefix,
    object_exists,
    upload_json_payload,
    v2_catalog_key,
)
from common.page_utils import parse_pages_filter
from common.tap_index import tap_payload_for_page


def _load_json(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_build_catalog" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {bucket}/{object_key}")
    return data


def _entry_from_toc_row(
    doc_id: str,
    row: dict[str, Any],
    *,
    page_no: int,
    printed_page: int | None,
    page_header: str | None,
    is_last_on_page: bool,
    page_entry_index: int,
    page_entry_count: int,
    tap: dict[str, Any] | None,
) -> dict[str, Any]:
    # Build STT entry with tap + OCR confidence / Tạo entry STT kèm tap + confidence OCR
    stt = int(row["stt"])
    tap_id = str(tap.get("tap_id")) if isinstance(tap, dict) and tap.get("tap_id") else None
    entry_id = make_entry_id(doc_id, stt, tap_id)
    trich = clean_trich_yeu(row.get("trich_yeu"))
    chi_muc = {
        "ngay_thang": row.get("ngay_thang"),
        "to_tap": row.get("to_tap"),
        "the_loai": row.get("the_loai"),
        "xuat_xu": row.get("xuat_xu"),
        "de_tai": row.get("de_tai") if str(row.get("de_tai") or "").strip().lower() not in {"none", "null"} else None,
    }
    flags = detect_entry_flags(chi_muc=chi_muc, trich_yeu=trich, is_last_on_page=is_last_on_page)
    ocr_confidence = estimate_entry_ocr_confidence(chi_muc=chi_muc, trich_yeu=trich, flags=flags)
    status = derive_entry_status(flags)

    content_alignment: list[dict[str, Any]] = []
    if trich["han_nom"] or trich["quoc_ngu"]:
        content_alignment.append(
            {
                "pair_index": 0,
                "pair_id": make_entry_pair_id(entry_id, 0),
                "han_nom": trich["han_nom"],
                "quoc_ngu": trich["quoc_ngu"],
                "source_page": page_no,
                "source_kind": "trich_yeu",
            }
        )
    return {
        "entry_id": entry_id,
        "doc_id": doc_id,
        "stt": stt,
        # Volume parent (triều + tập) — separate from chi_muc.to_tap /
        # Parent tập (triều + số tập) — tách biệt với chi_muc.to_tap
        "tap": tap,
        "chi_muc": chi_muc,
        "page_header": page_header,
        "trich_yeu": trich,
        "content_alignment": content_alignment,
        "text_views": {
            "han_nom_full": trich["han_nom"],
            "quoc_ngu_full": trich["quoc_ngu"],
        },
        "source_pages": [
            {
                "page_no": page_no,
                "printed_page": printed_page,
                "page_type": "muc_luc",
                "page_entry_index": page_entry_index,
                "page_entry_count": page_entry_count,
                "is_last_on_page": is_last_on_page,
            }
        ],
        "flags": flags,
        "ocr_confidence": ocr_confidence,
        "refine_confidence": None,
        "confidence_method": CONFIDENCE_METHOD,
        "status": status,
        "pipeline_version": PIPELINE_VERSION,
        "updated_at": utc_now_iso(),
    }


def build_catalog_from_ocr(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
) -> dict[str, Any]:
    """Merge TOC OCR pages into catalog + per-STT entry files (scoped by tap).

    Gộp trang OCR mục lục thành catalog + file entry theo STT trong từng tập.
    """
    cfg = load_config()
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    bucket_catalog = get_value(cfg, "minio", "bucket_catalog", fallback="hvb-catalog")
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    page_filter = parse_pages_filter(pages)

    object_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket_ocr, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("page_")
    ]

    # Merge into existing entries on partial batch runs /
    # Gộp vào entry sẵn có khi chạy từng batch
    merged: dict[str, dict[str, Any]] = {}
    source_pages: list[dict[str, Any]] = []
    if upload_minio:
        existing_keys = [
            key
            for key in list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/", suffix=".json")
            if Path(key).name.startswith("stt_")
        ]
        for key in existing_keys:
            try:
                existing_entry = _load_json(bucket_entries, key)
            except Exception as exc:
                print(f"[build_catalog] skip corrupt entry {key}: {exc}")
                continue
            eid = str(existing_entry.get("entry_id") or "")
            if eid:
                merged[eid] = existing_entry
        if object_exists(bucket_catalog, v2_catalog_key(doc_id)):
            try:
                existing_cat = _load_json(bucket_catalog, v2_catalog_key(doc_id))
                source_pages = list(existing_cat.get("source_pages") or [])
            except Exception as exc:
                print(f"[build_catalog] cannot load existing catalog pages: {exc}")

    for object_key in object_keys:
        page_no = int(Path(object_key).stem.split("_")[1])
        if page_filter is not None and page_no not in page_filter:
            continue
        payload = _load_json(bucket_ocr, object_key)
        page_type = str(payload.get("page_type") or "").lower()
        if page_type in {"blank", "tap_parent"}:
            print(f"[build_catalog] skip {page_type} page={page_no}")
            # Keep parent/blank markers in source_pages / Giữ marker parent/blank trong source_pages
            source_pages = [s for s in source_pages if not (isinstance(s, dict) and s.get("page_no") == page_no)]
            source_pages.append(
                {
                    "page_no": page_no,
                    "page_type": page_type,
                    "tap": payload.get("tap"),
                    "source_ocr_key": f"{bucket_ocr}/{object_key}",
                }
            )
            continue
        entries = payload.get("entries")
        if page_type not in {"muc_luc", "toc", "catalog"} and not isinstance(entries, list):
            print(f"[build_catalog] skip non-TOC page: {object_key}")
            continue
        if not isinstance(entries, list) or not entries:
            print(f"[build_catalog] skip empty TOC page: {object_key}")
            continue

        printed_page = payload.get("printed_page")
        page_header = payload.get("page_header")
        tap = payload.get("tap") if isinstance(payload.get("tap"), dict) else tap_payload_for_page(doc_id, page_no)
        valid_rows = [row for row in entries if isinstance(row, dict) and row.get("stt") is not None]
        source_pages = [s for s in source_pages if not (isinstance(s, dict) and s.get("page_no") == page_no)]
        source_pages.append(
            {
                "page_no": page_no,
                "printed_page": printed_page,
                "page_header": page_header,
                "tap": tap,
                "entry_count": len(valid_rows),
                "source_ocr_key": f"{bucket_ocr}/{object_key}",
            }
        )
        for index, row in enumerate(valid_rows):
            is_last = index == len(valid_rows) - 1
            entry = _entry_from_toc_row(
                doc_id,
                row,
                page_no=page_no,
                printed_page=printed_page if isinstance(printed_page, int) else None,
                page_header=page_header if isinstance(page_header, str) else None,
                is_last_on_page=is_last,
                page_entry_index=index,
                page_entry_count=len(valid_rows),
                tap=tap,
            )
            merged[str(entry["entry_id"])] = entry

    def _sort_key(item: dict[str, Any]) -> tuple:
        tap = item.get("tap") if isinstance(item.get("tap"), dict) else {}
        return (
            int(tap.get("parent_page_no") or 0),
            str(tap.get("tap_id") or ""),
            int(item.get("stt") or 0),
        )

    ordered = sorted(merged.values(), key=_sort_key)
    catalog = {
        "doc_id": doc_id,
        "pipeline_version": PIPELINE_VERSION,
        "built_at": utc_now_iso(),
        "entry_count": len(ordered),
        "source_pages": sorted(
            source_pages,
            key=lambda row: int(row.get("page_no") or 0) if isinstance(row, dict) else 0,
        ),
        "entries": [
            {
                "entry_id": item["entry_id"],
                "stt": item["stt"],
                "tap": item.get("tap"),
                "chi_muc": item.get("chi_muc"),
                "source_pages": item.get("source_pages"),
                "status": item.get("status"),
                "flags": item.get("flags"),
                "ocr_confidence": item.get("ocr_confidence"),
            }
            for item in ordered
        ],
    }

    if upload_minio:
        catalog_uri = upload_json_payload(bucket_catalog, v2_catalog_key(doc_id), catalog)
        print(f"[build_catalog] catalog -> {catalog_uri} entries={len(ordered)}")
        for item in ordered:
            # Only rewrite entries touched by this page filter /
            # Chỉ ghi lại entry thuộc filter trang hiện tại
            if page_filter is not None:
                page_nos = {
                    int(row.get("page_no"))
                    for row in (item.get("source_pages") or [])
                    if isinstance(row, dict) and row.get("page_no") is not None
                }
                if page_nos.isdisjoint(page_filter):
                    continue
            if "trich_yeu" not in item:
                # Catalog stub without body — skip upload of incomplete merge /
                # Stub catalog thiếu body — bỏ upload merge chưa đầy đủ
                continue
            entry_uri = upload_json_payload(
                bucket_entries,
                entry_object_key(doc_id, item),
                item,
            )
            print(
                f"[build_catalog] entry -> {entry_uri} "
                f"tap={((item.get('tap') or {}).get('tap_id'))} "
                f"conf={item.get('ocr_confidence')} flags={item.get('flags')}"
            )

    return catalog
