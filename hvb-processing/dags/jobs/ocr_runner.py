from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

from common.config import get_output_dir, load_config
from common.io_pdf import list_pdf_files
from common.io_storage import (
    download_object,
    list_objects_with_prefix,
    upload_output_dir,
    upload_page_ocr_result,
)
from common.schema import OcrResult

SUPPORTED_MODELS = ("paddle", "kandianguji", "google_vision", "chatgpt", "gemini")
MODEL_OUTPUT_FOLDERS: dict[str, str] = {
    "paddle": "paddle",
    "kandianguji": "kandianguji",
    "google_vision": "google_vision",
    "chatgpt": "chatgpt",
    "gemini": "gemini",
}


def run_single_model(model: str, pdf_path: Path) -> list[OcrResult]:
    # Dynamically load model adapter / Nạp model adapter động theo tên model
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model '{model}'. Choose from: {SUPPORTED_MODELS}")
    module = importlib.import_module(f"ocr_{model}")
    return module.run(pdf_path)


def save_page_result(
    result: OcrResult,
    model_folder: str,
    output_dir: str | Path | None = None,
) -> Path:
    # Persist one JSON file per page under model folder / Lưu một JSON mỗi trang trong folder model
    base_dir = Path(output_dir) if output_dir else get_output_dir()
    page_dir = base_dir / model_folder / result.doc_id
    page_dir.mkdir(parents=True, exist_ok=True)
    output_file = page_dir / f"page_{result.page_no:04d}.json"
    output_file.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_file


def save_results(results: list[OcrResult], output_dir: str | Path | None = None, output_suffix: str = "") -> Path:
    # Persist one JSON per document-model / Lưu một JSON cho mỗi cặp document-model
    base_dir = Path(output_dir) if output_dir else get_output_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    first = results[0]
    output_file = base_dir / f"{first.doc_id}_{first.model_name}{output_suffix}.json"
    payload = [item.to_dict() for item in results]
    output_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_file


def parse_pages_filter(raw: str | list[int] | None) -> set[int] | None:
    # Parse page filter string like "1,3,5" or "1-10" / Parse chuỗi lọc trang như "1,3,5" hoặc "1-10"
    if raw is None:
        return None
    if isinstance(raw, list):
        return {int(page) for page in raw if str(page).strip()}
    text = str(raw).strip()
    if not text:
        return None

    pages: set[int] = set()
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if end < start:
                raise ValueError(f"Invalid page range '{token}': end < start")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(token))
    return pages or None


def pages_filter_suffix(page_filter: set[int] | None) -> str:
    # Build filename suffix for partial page runs / Tạo hậu tố tên file khi chỉ chạy một số trang
    if not page_filter:
        return ""
    ordered = sorted(page_filter)
    if len(ordered) > 1 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"_p{ordered[0]}-{ordered[-1]}"
    return "_p" + "-".join(str(page) for page in ordered)


def run_one(model: str, pdf_path: str) -> Path:
    # Process one PDF with one model / Xử lý một PDF bằng một model
    cfg = load_config()
    results = run_single_model(model=model, pdf_path=Path(pdf_path))
    return save_results(results, output_dir=get_output_dir())


def run_batch(model: str, upload_minio: bool = False) -> list[str]:
    # Process all PDFs from configured raw folder / Xử lý toàn bộ PDF trong thư mục raw
    cfg = load_config()
    raw_dir = cfg.get("paths", "raw_dir")
    output_dir = get_output_dir()
    output_files: list[str] = []
    for pdf_path in list_pdf_files(raw_dir):
        output_file = run_one(model=model, pdf_path=str(pdf_path))
        output_files.append(str(output_file))
        print(f"[{model}] {pdf_path.name} -> {output_file}")
    if upload_minio:
        uploaded = upload_output_dir(str(output_dir))
        print(f"Uploaded {len(uploaded)} file(s) to MinIO")
        return uploaded
    return output_files


def run_compare(models: list[str], upload_minio: bool = False) -> None:
    # Execute same dataset across many models / Chạy cùng dataset trên nhiều model
    for model in models:
        run_batch(model=model, upload_minio=upload_minio)


def _load_manifest(bucket_raw: str, manifest_key: str) -> dict:
    # Download manifest JSON from MinIO / Tải manifest JSON từ MinIO
    local_manifest = download_object(
        bucket=bucket_raw,
        object_name=manifest_key,
        local_path=Path("/tmp") / "hvb_manifests" / Path(manifest_key).name,
    )
    try:
        return json.loads(local_manifest.read_text(encoding="utf-8"))
    finally:
        if local_manifest.exists():
            local_manifest.unlink()


def _run_doc_pages_from_manifest(
    model: str,
    bucket_raw: str,
    manifest: dict,
    output_dir: str | Path | None = None,
    page_filter: set[int] | None = None,
) -> Path | None:
    # OCR one document by iterating split pages listed in manifest / OCR một tài liệu bằng danh sách trang trong manifest
    doc_id = manifest.get("doc_id", "unknown_doc")
    pages = manifest.get("pages", [])
    if not pages:
        return None

    doc_results: list[OcrResult] = []
    for page in pages:
        page_no = int(page.get("page_no", 1))
        if page_filter is not None and page_no not in page_filter:
            continue
        object_key = page["object_key"]
        local_page = download_object(
            bucket=bucket_raw,
            object_name=object_key,
            local_path=Path("/tmp") / "hvb_pages" / doc_id / Path(object_key).name,
        )
        try:
            page_results = run_single_model(model=model, pdf_path=local_page)
            for item in page_results:
                # Normalize metadata to original document/page context / Chuẩn hóa metadata theo tài liệu/trang gốc
                item.doc_id = doc_id
                item.page_no = page_no
                item.source_pdf = f"{bucket_raw}/{object_key}"
            doc_results.extend(page_results)
        finally:
            if local_page.exists():
                local_page.unlink()

    if not doc_results:
        return None
    return save_results(
        doc_results,
        output_dir=output_dir,
        output_suffix=pages_filter_suffix(page_filter),
    )


def _resolve_manifest_for_doc(
    bucket_raw: str,
    manifest_prefix: str,
    doc_id: str,
) -> tuple[str, dict]:
    # Find split manifest for one document / Tìm manifest split cho một tài liệu
    manifest_keys = list_objects_with_prefix(bucket=bucket_raw, prefix=manifest_prefix, suffix=".json")
    for manifest_key in manifest_keys:
        manifest = _load_manifest(bucket_raw=bucket_raw, manifest_key=manifest_key)
        if manifest.get("doc_id") == doc_id:
            return manifest_key, manifest
    raise ValueError(f"No manifest found for doc_id='{doc_id}' under prefix '{manifest_prefix}'")


def run_single_page_from_split_minio(
    model: str,
    doc_id: str,
    page_no: int,
    *,
    upload_minio: bool = True,
    model_folder: str | None = None,
) -> str:
    # OCR one page and optionally upload per-page JSON / OCR một trang và upload JSON riêng
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model '{model}'. Choose from: {SUPPORTED_MODELS}")

    cfg = load_config()
    bucket_raw = cfg.get("minio", "bucket_raw")
    manifest_prefix = cfg.get("minio", "manifest_prefix")
    folder = model_folder or MODEL_OUTPUT_FOLDERS.get(model, model)

    _manifest_key, manifest = _resolve_manifest_for_doc(bucket_raw, manifest_prefix, doc_id)
    page_entry = next(
        (page for page in manifest.get("pages", []) if int(page.get("page_no", 0)) == page_no),
        None,
    )
    if page_entry is None:
        raise ValueError(f"Page {page_no} not found in manifest for doc_id='{doc_id}'")

    object_key = page_entry["object_key"]
    local_page = download_object(
        bucket=bucket_raw,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_pages" / doc_id / Path(object_key).name,
    )
    try:
        page_results = run_single_model(model=model, pdf_path=local_page)
        if not page_results:
            raise RuntimeError(f"OCR returned no result for {doc_id} page {page_no}")
        result = page_results[0]
        result.doc_id = doc_id
        result.page_no = page_no
        result.source_pdf = f"{bucket_raw}/{object_key}"

        if model == "paddle":
            from common.ollama_refine import maybe_refine_paddle_metadata

            result = maybe_refine_paddle_metadata(result, cfg=cfg)

        if upload_minio:
            minio_uri = upload_page_ocr_result(
                result.to_dict(),
                model_folder=folder,
                doc_id=doc_id,
                page_no=page_no,
            )
            print(f"[{model}] uploaded -> {minio_uri}")
            return minio_uri

        output_file = save_page_result(result, model_folder=folder)
        print(f"[{model}] {doc_id} page {page_no} -> {output_file}")
        return str(output_file)
    finally:
        if local_page.exists():
            local_page.unlink()


def run_page_loop_from_split_minio(
    model: str,
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
    model_folder: str | None = None,
) -> list[str]:
    # Loop OCR pages one-by-one, writing one JSON per page / Lặp OCR từng trang, mỗi trang một JSON
    cfg = load_config()
    bucket_raw = cfg.get("minio", "bucket_raw")
    manifest_prefix = cfg.get("minio", "manifest_prefix")
    page_filter = parse_pages_filter(pages)

    _manifest_key, manifest = _resolve_manifest_for_doc(bucket_raw, manifest_prefix, doc_id)
    manifest_pages = manifest.get("pages", [])
    if not manifest_pages:
        raise ValueError(f"Manifest for doc_id='{doc_id}' has no pages")

    page_numbers = sorted(int(page.get("page_no", 0)) for page in manifest_pages)
    if page_filter is not None:
        page_numbers = [page_no for page_no in page_numbers if page_no in page_filter]
    if not page_numbers:
        raise ValueError(f"No pages to OCR for doc_id='{doc_id}' with filter={pages!r}")

    page_delay_sec = 0.0
    if model == "gemini":
        from common.config import get_value

        page_delay_sec = float(get_value(cfg, "gemini", "page_delay_sec", fallback="20"))

    outputs: list[str] = []
    for index, page_no in enumerate(page_numbers):
        outputs.append(
            run_single_page_from_split_minio(
                model=model,
                doc_id=doc_id,
                page_no=page_no,
                upload_minio=upload_minio,
                model_folder=model_folder,
            )
        )
        # Pace Gemini requests to avoid burst rate limits / Giãn request Gemini để tránh vượt rate limit
        if page_delay_sec > 0 and index + 1 < len(page_numbers):
            time.sleep(page_delay_sec)
    print(f"[{model}] completed {len(outputs)} page(s) for doc_id='{doc_id}'")
    return outputs


def run_batch_from_split_minio(
    model: str,
    upload_minio: bool = True,
    doc_id: str | None = None,
    pages: str | list[int] | None = None,
) -> list[str]:
    # OCR pages from MinIO split manifests / OCR từ manifest trang đã split trên MinIO
    cfg = load_config()
    bucket_raw = cfg.get("minio", "bucket_raw")
    manifest_prefix = cfg.get("minio", "manifest_prefix")
    output_dir = get_output_dir()
    page_filter = parse_pages_filter(pages)
    doc_id_filter = (doc_id or "").strip() or None

    manifest_keys = list_objects_with_prefix(bucket=bucket_raw, prefix=manifest_prefix, suffix=".json")
    output_files: list[str] = []
    for manifest_key in manifest_keys:
        manifest = _load_manifest(bucket_raw=bucket_raw, manifest_key=manifest_key)
        manifest_doc_id = manifest.get("doc_id", "")
        if doc_id_filter and manifest_doc_id != doc_id_filter:
            continue
        output_file = _run_doc_pages_from_manifest(
            model=model,
            bucket_raw=bucket_raw,
            manifest=manifest,
            output_dir=output_dir,
            page_filter=page_filter,
        )
        if output_file:
            output_files.append(str(output_file))
            print(f"[{model}] {manifest_key} -> {output_file}")

    if upload_minio:
        uploaded = upload_output_dir(str(output_dir))
        print(f"Uploaded {len(uploaded)} file(s) to MinIO")
        return uploaded
    return output_files


def run_compare_from_split_minio(
    models: list[str],
    upload_minio: bool = True,
    doc_id: str | None = None,
    pages: str | list[int] | None = None,
) -> None:
    # Compare models on split pages from MinIO manifests / So sánh model trên trang đã split từ MinIO
    for model in models:
        run_batch_from_split_minio(
            model=model,
            upload_minio=upload_minio,
            doc_id=doc_id,
            pages=pages,
        )
