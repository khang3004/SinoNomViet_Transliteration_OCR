from __future__ import annotations

import os
import time
from pathlib import Path

from common.artifact_state import should_skip_api_call
from common.chau_ban_schema import (
    PIPELINE_VERSION,
    parse_ocr_structured_response,
    parse_ocr_toc_response,
    utc_now_iso,
)
from common.config import get_value, load_config
from common.io_storage import (
    download_object,
    upload_json_payload,
    v2_ocr_page_key,
    v2_preprocessed_page_key,
)
from common.ocr_confidence import estimate_gemini_ocr_confidence
from common.tap_index import (
    is_blank_page,
    is_tap_parent_page,
    parent_page_map,
    tap_payload_for_page,
)
from ocr_gemini_opencv import recognize_gemini_opencv, recognize_toc_bottom_retry
from common.page_utils import parse_pages_filter, resolve_manifest_for_doc

OCR_MODEL = "gemini-3.5-flash-low"
OCR_PROVIDER = "ramclouds"


def _entry_trich_empty(entry: dict) -> bool:
    # True when both TOC columns empty / True khi cả hai cột trích yếu đều rỗng
    trich = entry.get("trich_yeu") if isinstance(entry, dict) else None
    if not isinstance(trich, dict):
        return True
    hn = str(trich.get("han_nom") or "").strip()
    qn = str(trich.get("quoc_ngu") or "").strip()
    if hn.lower() in {"none", "null"}:
        hn = ""
    if qn.lower() in {"none", "null"}:
        qn = ""
    return not hn and not qn


def _meta_missing(entry: dict) -> bool:
    # True when catalog fields still empty on cut entry /
    # True khi field chỉ mục còn trống trên entry bị cắt
    if not isinstance(entry, dict):
        return True
    for key in ("to_tap", "the_loai", "xuat_xu", "de_tai"):
        val = entry.get(key)
        if val is None or str(val).strip() in {"", "None", "null"}:
            return True
    return False


def _needs_bottom_retry(entry: dict) -> bool:
    # Retry crop if last entry incomplete (not only date) /
    # Crop lại nếu entry cuối cụt (không chỉ còn dòng Ngày)
    return _entry_trich_empty(entry) or _meta_missing(entry)


def _merge_bottom_retry_into_toc(structured: dict, retry_structured: dict) -> dict:
    """Fill incomplete last entry from bottom-crop OCR (metadata + TY).

    Bổ sung entry cuối cụt bằng OCR crop phần dưới (metadata + TY).
    """
    entries = list(structured.get("entries") or [])
    retry_entries = list(retry_structured.get("entries") or [])
    if not entries or not retry_entries:
        return structured
    last = entries[-1]
    if not isinstance(last, dict) or not _needs_bottom_retry(last):
        return structured
    # Prefer matching STT; else take sole retry entry / Ưu tiên khớp STT; không thì lấy entry retry
    candidate = None
    last_stt = last.get("stt")
    for row in retry_entries:
        if not isinstance(row, dict):
            continue
        if last_stt is not None and row.get("stt") == last_stt:
            candidate = row
            break
    if candidate is None:
        candidate = retry_entries[0] if isinstance(retry_entries[0], dict) else None
    if candidate is None:
        return structured
    merged = dict(last)
    filled = False
    for key in ("ngay_thang", "to_tap", "the_loai", "xuat_xu", "de_tai"):
        cur = merged.get(key)
        nxt = candidate.get(key)
        empty = cur is None or str(cur).strip() in {"", "None", "null"}
        if empty and nxt is not None and str(nxt).strip() not in {"", "None", "null"}:
            merged[key] = nxt
            filled = True
    if _entry_trich_empty(merged) and not _entry_trich_empty(candidate):
        merged["trich_yeu"] = candidate.get("trich_yeu")
        filled = True
    if not filled:
        return structured
    entries[-1] = merged
    out = dict(structured)
    out["entries"] = entries
    out["bottom_retry"] = True
    return out


def resolve_page_kind(page_kind: str | None = None) -> str:
    # Resolve toc|body from arg/env/config / Xác định toc|body từ arg/env/config
    if page_kind and str(page_kind).strip():
        kind = str(page_kind).strip().lower()
    else:
        kind = (
            os.environ.get("HVB_PAGE_KIND")
            or get_value(load_config(), "pipeline", "default_page_kind", fallback="toc")
        ).strip().lower()
    if kind in {"toc", "muc_luc", "catalog"}:
        return "toc"
    return "body"


def ocr_single_page_v2(
    doc_id: str,
    page_no: int,
    *,
    upload_minio: bool = True,
    page_kind: str | None = None,
) -> str:
    # OCR one preprocessed PNG; skip API when MinIO JSON exists / OCR một PNG; bỏ qua API nếu đã có JSON
    cfg = load_config()
    bucket_preprocessed = get_value(cfg, "minio", "bucket_preprocessed", fallback="hvb-preprocessed")
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    preprocess_version = get_value(
        cfg, "opencv_preprocess", "preprocess_version", fallback="opencv_wm_light_v2"
    )
    ocr_model = get_value(cfg, "gemini_opencv", "model", fallback=OCR_MODEL)
    kind = resolve_page_kind(page_kind)
    object_key = v2_ocr_page_key(doc_id, page_no)

    # Persist state: reuse stored OCR JSON to avoid re-billing / Giữ state: dùng lại JSON OCR để tránh gọi API lại
    if upload_minio and should_skip_api_call(bucket_ocr, object_key):
        return f"{bucket_ocr}/{object_key}"

    # Skip Gemini for blank / tap-parent divider pages /
    # Bỏ Gemini cho trang trắng / trang chia tập (parent)
    if kind == "toc" and is_blank_page(doc_id, page_no):
        payload = {
            "doc_id": doc_id,
            "page_no": page_no,
            "page_type": "blank",
            "skipped": True,
            "skip_reason": "blank_page",
            "tap": tap_payload_for_page(doc_id, page_no),
            "entries": [],
            "entry_count": 0,
            "parse_ok": True,
            "confidence": 1.0,
            "ocr_model": "rule:blank",
            "ocr_provider": "local",
            "ocr_at": utc_now_iso(),
            "pipeline_version": PIPELINE_VERSION,
            "elapsed_ms": 0,
        }
        if upload_minio:
            uri = upload_json_payload(bucket_ocr, object_key, payload)
            print(f"[ocr_v2] blank skip -> {uri}")
            return uri
        return json_local_fallback(payload, doc_id, page_no)

    if kind == "toc" and is_tap_parent_page(doc_id, page_no):
        tap = parent_page_map(doc_id).get(page_no) or tap_payload_for_page(doc_id, page_no)
        payload = {
            "doc_id": doc_id,
            "page_no": page_no,
            "page_type": "tap_parent",
            "skipped": True,
            "skip_reason": "tap_parent",
            "tap": tap,
            "page_header": "MỤC LỤC CHÂU BẢN TRIỀU NGUYỄN",
            "entries": [],
            "entry_count": 0,
            "parse_ok": True,
            "confidence": 1.0,
            "ocr_model": "rule:tap_index",
            "ocr_provider": "local",
            "ocr_at": utc_now_iso(),
            "pipeline_version": PIPELINE_VERSION,
            "elapsed_ms": 0,
        }
        if upload_minio:
            uri = upload_json_payload(bucket_ocr, object_key, payload)
            print(f"[ocr_v2] tap_parent skip -> {uri} tap={tap.get('tap_id') if tap else None}")
            return uri
        return json_local_fallback(payload, doc_id, page_no)

    png_key = v2_preprocessed_page_key(doc_id, page_no)
    local_png = download_object(
        bucket=bucket_preprocessed,
        object_name=png_key,
        local_path=Path("/tmp") / "hvb_v2_preprocessed" / doc_id / Path(png_key).name,
    )
    try:
        started = time.perf_counter()
        png_bytes = local_png.read_bytes()
        raw_text, confidence, _blocks = recognize_gemini_opencv(
            png_bytes,
            page_kind=kind,
        )
        if kind == "toc":
            structured = parse_ocr_toc_response(raw_text)
            entries = structured.get("entries") or []
            # Retry bottom crop when last STT incomplete (meta or TY) /
            # Thử OCR phần dưới nếu STT cuối còn thiếu metadata hoặc TY
            if entries and isinstance(entries[-1], dict) and _needs_bottom_retry(entries[-1]):
                print(f"[ocr_v2] page={page_no} bottom retry for incomplete last entry")
                try:
                    retry_raw, retry_conf, _ = recognize_toc_bottom_retry(png_bytes)
                    retry_structured = parse_ocr_toc_response(retry_raw)
                    structured = _merge_bottom_retry_into_toc(structured, retry_structured)
                    raw_text = f"{raw_text}\n\n---BOTTOM_RETRY---\n{retry_raw}"
                    confidence = max(confidence, retry_conf)
                except Exception as exc:
                    print(f"[ocr_v2] bottom retry failed page={page_no}: {exc}")
            payload = {
                "doc_id": doc_id,
                "page_no": page_no,
                "page_type": "muc_luc",
                "page_header": structured.get("page_header"),
                "printed_page": structured.get("printed_page"),
                # Volume parent covering this TOC page / Tập parent bao phủ trang TOC này
                "tap": tap_payload_for_page(doc_id, page_no),
                "orphan_head": structured.get("orphan_head") or {"han_nom": "", "quoc_ngu": ""},
                "entry_continuation": structured.get("entry_continuation"),
                "entries": structured.get("entries", []),
                "entry_count": len(structured.get("entries") or []),
                "raw_text": raw_text,
                "parse_ok": structured.get("parse_ok", False),
                "bottom_retry": bool(structured.get("bottom_retry")),
                "confidence": confidence if confidence > 0 else estimate_gemini_ocr_confidence(raw_text),
                "ocr_model": ocr_model,
                "ocr_provider": OCR_PROVIDER,
                "ocr_at": utc_now_iso(),
                "source_png_key": f"{bucket_preprocessed}/{png_key}",
                "preprocess_version": preprocess_version,
                "pipeline_version": PIPELINE_VERSION,
            }
        else:
            structured = parse_ocr_structured_response(raw_text)
            if confidence <= 0:
                confidence = estimate_gemini_ocr_confidence(raw_text)
            payload = {
                "doc_id": doc_id,
                "page_no": page_no,
                "page_type": "body",
                "page_header": structured.get("page_header"),
                "printed_page": structured.get("printed_page"),
                "ngay_thang": structured.get("ngay_thang"),
                "the_loai": structured.get("the_loai"),
                "de_tai": structured.get("de_tai"),
                "blocks": structured.get("blocks", []),
                "raw_text": raw_text,
                "parse_ok": structured.get("parse_ok", False),
                "confidence": confidence,
                "ocr_model": ocr_model,
                "ocr_provider": OCR_PROVIDER,
                "ocr_at": utc_now_iso(),
                "source_png_key": f"{bucket_preprocessed}/{png_key}",
                "preprocess_version": preprocess_version,
                "pipeline_version": PIPELINE_VERSION,
            }
        # Shared elapsed timing for both page kinds / Đo thời gian chung cho cả toc và body
        payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)

        if upload_minio:
            uri = upload_json_payload(bucket_ocr, object_key, payload)
            print(
                f"[ocr_v2] uploaded -> {uri} kind={kind} "
                f"entries={payload.get('entry_count', 'n/a')}"
            )
            return uri
        return json_local_fallback(payload, doc_id, page_no)
    finally:
        if local_png.exists():
            local_png.unlink(missing_ok=True)


def json_local_fallback(payload: dict, doc_id: str, page_no: int) -> str:
    # Write OCR JSON locally when MinIO upload disabled / Ghi JSON OCR local khi tắt upload MinIO
    import json

    out_dir = Path("/tmp") / "hvb_ocr_v2" / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"page_{page_no:04d}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out_file)


def ocr_page_loop_v2(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
    page_kind: str | None = None,
) -> list[str]:
    # Loop structured OCR for selected pages / Lặp OCR có cấu trúc theo trang chọn
    cfg = load_config()
    bucket_raw = get_value(cfg, "minio", "bucket_raw")
    manifest_prefix = get_value(cfg, "minio", "manifest_prefix")
    page_filter = parse_pages_filter(pages)
    page_delay_sec = float(get_value(cfg, "gemini_opencv", "page_delay_sec", fallback="5"))
    kind = resolve_page_kind(page_kind)

    _manifest_key, manifest = resolve_manifest_for_doc(bucket_raw, manifest_prefix, doc_id)
    page_numbers = sorted(int(page.get("page_no", 0)) for page in manifest.get("pages", []))
    if page_filter is not None:
        page_numbers = [page_no for page_no in page_numbers if page_no in page_filter]
    if not page_numbers:
        raise ValueError(f"No pages to OCR for doc_id='{doc_id}' filter={pages!r}")

    outputs: list[str] = []
    for index, page_no in enumerate(page_numbers):
        before_skip_env = os.environ.get("HVB_FORCE", "")
        object_key = v2_ocr_page_key(doc_id, page_no)
        bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
        from common.artifact_state import should_skip_api_call

        skipped = upload_minio and should_skip_api_call(bucket_ocr, object_key)
        outputs.append(
            ocr_single_page_v2(
                doc_id,
                page_no,
                upload_minio=upload_minio,
                page_kind=kind,
            )
        )
        # Delay only after a real vision API call / Chỉ delay sau lần gọi OCR thật
        if (
            not skipped
            and page_delay_sec > 0
            and index + 1 < len(page_numbers)
            and before_skip_env is not None
        ):
            time.sleep(page_delay_sec)
    print(f"[ocr_v2] completed {len(outputs)} page(s) for doc_id='{doc_id}' kind={kind}")
    return outputs
