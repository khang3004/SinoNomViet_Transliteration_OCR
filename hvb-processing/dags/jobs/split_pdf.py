from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure `common` package is importable when Airflow parses this file directly / Đảm bảo import được `common` khi Airflow parse trực tiếp file này
JOBS_DIR = os.path.dirname(os.path.abspath(__file__))
if JOBS_DIR not in sys.path:
    sys.path.append(JOBS_DIR)

from common.config import get_value, load_config
from common.io_storage import (
    download_object,
    list_objects_with_prefix,
    upload_files_with_prefix,
    upload_json_payload,
)


def _split_to_single_page_pdfs(pdf_path: Path, staging_dir: Path) -> list[Path]:
    # Split one PDF into single-page PDFs / Tách một PDF thành nhiều PDF một trang
    try:
        # Import lazily to avoid breaking DAG parsing / Import trễ để tránh vỡ DAG lúc parse
        from pypdf import PdfReader, PdfWriter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install it in Airflow runtime before split stage."
        ) from exc

    reader = PdfReader(str(pdf_path))
    output_files: list[Path] = []
    for index, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        page_file = staging_dir / f"page_{index:04d}.pdf"
        with page_file.open("wb") as handle:
            writer.write(handle)
        output_files.append(page_file)
    return output_files


def _split_and_upload_local_pdf(
    pdf_path: Path, bucket_raw: str, source_object_name: str, pages_prefix: str, manifest_prefix: str
) -> dict:
    # Split local PDF then upload pages + manifest / Tách PDF local rồi upload trang và manifest
    cfg = load_config()
    staging_root = Path(cfg.get("paths", "staging_dir"))
    doc_id = pdf_path.stem

    staging_dir = staging_root / doc_id
    # Ensure fresh staging for deterministic outputs / Làm mới staging để output ổn định
    if staging_dir.exists():
        for stale_file in staging_dir.glob("*.pdf"):
            stale_file.unlink()
    staging_dir.mkdir(parents=True, exist_ok=True)

    pages = _split_to_single_page_pdfs(pdf_path, staging_dir)
    page_object_prefix = f"{pages_prefix}/{doc_id}"
    uploaded_pages = upload_files_with_prefix(
        bucket=bucket_raw,
        local_dir=staging_dir,
        object_prefix=page_object_prefix,
        glob_pattern="*.pdf",
    )

    manifest = {
        "doc_id": doc_id,
        "source_pdf": f"{bucket_raw}/{source_object_name}",
        "total_pages": len(pages),
        "pages": [
            {
                "page_no": index,
                "object_key": f"{page_object_prefix}/page_{index:04d}.pdf",
            }
            for index in range(1, len(pages) + 1)
        ],
    }
    manifest_object_name = f"{manifest_prefix}/{doc_id}.json"
    manifest_uri = upload_json_payload(
        bucket=bucket_raw,
        object_name=manifest_object_name,
        payload=manifest,
    )

    # Clean temporary staging files after upload / Xóa file tạm sau khi upload
    for page_file in pages:
        if page_file.exists():
            page_file.unlink()

    return {
        "doc_id": doc_id,
        "uploaded_pages": uploaded_pages,
        "manifest_uri": manifest_uri,
    }


def sync_local_raw_to_minio() -> list[str]:
    # Upload local raw PDFs to MinIO source prefix / Upload PDF local lên source prefix của MinIO
    cfg = load_config()
    raw_prefix = get_value(cfg, "minio", "raw_prefix")
    bucket_raw = get_value(cfg, "minio", "bucket_raw")
    raw_dir = cfg.get("paths", "raw_dir")
    return upload_files_with_prefix(
        bucket=bucket_raw,
        local_dir=Path(raw_dir),
        object_prefix=raw_prefix,
        glob_pattern="*.pdf",
    )


def split_and_upload_batch_from_minio() -> list[dict]:
    # Split all source PDFs from MinIO and upload pages/manifest / Tách toàn bộ PDF nguồn từ MinIO rồi upload pages/manifest
    cfg = load_config()
    bucket_raw = get_value(cfg, "minio", "bucket_raw")
    raw_prefix = get_value(cfg, "minio", "raw_prefix")
    pages_prefix = get_value(cfg, "minio", "pages_prefix")
    manifest_prefix = get_value(cfg, "minio", "manifest_prefix")
    source_keys = list_objects_with_prefix(bucket=bucket_raw, prefix=raw_prefix, suffix=".pdf")

    results: list[dict] = []
    for source_key in source_keys:
        source_file = Path(source_key).name
        local_pdf = download_object(
            bucket=bucket_raw,
            object_name=source_key,
            local_path=Path("/tmp") / "hvb_split_source" / source_file,
        )
        result = _split_and_upload_local_pdf(
            pdf_path=local_pdf,
            bucket_raw=bucket_raw,
            source_object_name=source_key,
            pages_prefix=pages_prefix,
            manifest_prefix=manifest_prefix,
        )
        results.append(result)
        print(
            f"[split] {source_file} -> {len(result['uploaded_pages'])} pages, "
            f"manifest={result['manifest_uri']}"
        )
        # Cleanup downloaded source after split / Xóa file nguồn tải về sau khi tách trang
        if local_pdf.exists():
            local_pdf.unlink()
    return results


def split_and_upload_batch() -> list[dict]:
    # Backward-compatible entrypoint now splitting from MinIO source / Entrypoint tương thích ngược, giờ tách từ nguồn MinIO
    return split_and_upload_batch_from_minio()
