from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from common.align_client import align_ocr_page_with_deepseek
from common.artifact_state import should_skip_api_call
from common.chau_ban_schema import PIPELINE_VERSION, make_pair_id, utc_now_iso
from common.config import get_value, load_config
from common.io_storage import (
    download_object,
    list_objects_with_prefix,
    upload_json_payload,
    v2_aligned_page_key,
    v2_ocr_page_key,
)
from common.page_utils import parse_pages_filter


def _load_json(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_align_v2" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {bucket}/{object_key}")
    return data


def align_single_page_v2(
    doc_id: str,
    page_no: int,
    *,
    upload_minio: bool = True,
) -> dict[str, Any] | None:
    # Align one OCR body page into bilingual pairs / Dóng hàng một trang OCR thân văn thành cặp song ngữ
    cfg = load_config()
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    bucket_aligned = get_value(cfg, "minio", "bucket_aligned", fallback="hvb-aligned")
    aligned_key = v2_aligned_page_key(doc_id, page_no)

    if upload_minio and should_skip_api_call(bucket_aligned, aligned_key):
        return _load_json(bucket_aligned, aligned_key)

    ocr_key = v2_ocr_page_key(doc_id, page_no)
    ocr_payload = _load_json(bucket_ocr, ocr_key)
    page_type = str(ocr_payload.get("page_type") or "").lower()
    # TOC pages use catalog/trich_yeu — skip DeepSeek align / Trang mục lục dùng catalog — bỏ align DeepSeek
    if page_type in {"muc_luc", "toc", "catalog"} or isinstance(ocr_payload.get("entries"), list):
        print(f"[align_v2] skip TOC page {page_no} (use build_catalog)")
        return None

    pairs, align_model = align_ocr_page_with_deepseek(ocr_payload)

    content_alignment: list[dict[str, Any]] = []
    for pair_index, pair in enumerate(pairs):
        content_alignment.append(
            {
                "pair_index": pair_index,
                "pair_id": make_pair_id(doc_id, page_no, pair_index),
                "han_nom": pair.get("han_nom", ""),
                "quoc_ngu": pair.get("quoc_ngu", ""),
            }
        )

    aligned_payload = {
        "doc_id": doc_id,
        "page_no": page_no,
        "page_type": "body",
        "page_header": ocr_payload.get("page_header"),
        "printed_page": ocr_payload.get("printed_page"),
        "ngay_thang": ocr_payload.get("ngay_thang"),
        "the_loai": ocr_payload.get("the_loai"),
        "de_tai": ocr_payload.get("de_tai"),
        "content_alignment": content_alignment,
        "ocr_model": ocr_payload.get("ocr_model"),
        "ocr_provider": ocr_payload.get("ocr_provider"),
        "ocr_at": ocr_payload.get("ocr_at"),
        "align_model": align_model,
        "align_at": utc_now_iso(),
        "source_ocr_key": f"{bucket_ocr}/{ocr_key}",
        "source_png_key": ocr_payload.get("source_png_key"),
        "preprocess_version": ocr_payload.get("preprocess_version"),
        "pipeline_version": PIPELINE_VERSION,
        "pair_count": len(content_alignment),
    }

    if upload_minio:
        uri = upload_json_payload(bucket_aligned, aligned_key, aligned_payload)
        print(f"[align_v2] uploaded -> {uri} pairs={len(content_alignment)}")
    return aligned_payload


def align_page_loop_v2(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
) -> list[str]:
    # Loop align over OCR JSONs in hvb-ocr / Lặp align trên JSON OCR trong hvb-ocr
    cfg = load_config()
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    page_filter = parse_pages_filter(pages)
    page_delay_sec = float(get_value(cfg, "align", "page_delay_sec", fallback="3"))

    prefix = f"{doc_id}/"
    object_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket_ocr, prefix=prefix, suffix=".json")
        if Path(key).name.startswith("page_")
    ]

    outputs: list[str] = []
    for index, object_key in enumerate(object_keys):
        page_no = int(Path(object_key).stem.split("_")[1])
        if page_filter is not None and page_no not in page_filter:
            continue
        result = align_single_page_v2(doc_id, page_no, upload_minio=upload_minio)
        if result is not None:
            outputs.append(object_key)
            if page_delay_sec > 0 and index + 1 < len(object_keys):
                time.sleep(page_delay_sec)

    print(f"[align_v2] completed {len(outputs)} body page(s) for doc_id='{doc_id}'")
    return outputs
