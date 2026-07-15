from __future__ import annotations

from pathlib import Path

from common.artifact_state import should_skip_api_call
from common.config import get_value, load_config
from common.io_storage import (
    download_object,
    preprocessed_page_object_key,
    upload_png_bytes,
)
from common.opencv_denoise import denoise_png_bytes
from common.page_utils import parse_pages_filter, resolve_manifest_for_doc
from common.preprocess import get_render_dpi, pdf_to_png_bytes


def preprocess_single_page_from_split_minio(
    doc_id: str,
    page_no: int,
    *,
    upload_minio: bool = True,
) -> str:
    # Render split PDF page, denoise with OpenCV, upload PNG / Render PDF, lọc nhiễu OpenCV, upload PNG
    cfg = load_config()
    bucket_raw = cfg.get("minio", "bucket_raw")
    manifest_prefix = cfg.get("minio", "manifest_prefix")
    png_key = preprocessed_page_object_key(doc_id, page_no)
    bucket_preprocessed = get_value(cfg, "minio", "bucket_preprocessed", fallback="hvb-preprocessed")

    # Skip expensive render/denoise when PNG already stored / Bỏ render/denoise nếu PNG đã lưu
    if upload_minio and should_skip_api_call(bucket_preprocessed, png_key):
        return f"{bucket_preprocessed}/{png_key}"

    _manifest_key, manifest = resolve_manifest_for_doc(bucket_raw, manifest_prefix, doc_id)
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
        render_dpi = int(get_value(cfg, "opencv_preprocess", "render_dpi", fallback=str(get_render_dpi())))
        raw_png = pdf_to_png_bytes(local_page, dpi=render_dpi)
        denoised_png = denoise_png_bytes(raw_png)

        if upload_minio:
            minio_uri = upload_png_bytes(bucket_preprocessed, png_key, denoised_png)
            print(f"[opencv_preprocess] uploaded -> {minio_uri}")
            return minio_uri

        output_dir = Path("/tmp") / "hvb_preprocessed" / doc_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"page_{page_no:04d}.png"
        output_file.write_bytes(denoised_png)
        print(f"[opencv_preprocess] {doc_id} page {page_no} -> {output_file}")
        return str(output_file)
    finally:
        if local_page.exists():
            local_page.unlink()


def preprocess_page_loop_from_split_minio(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
) -> list[str]:
    # Loop preprocess for manifest pages / Lặp preprocess cho các trang trong manifest
    cfg = load_config()
    bucket_raw = cfg.get("minio", "bucket_raw")
    manifest_prefix = cfg.get("minio", "manifest_prefix")
    page_filter = parse_pages_filter(pages)

    _manifest_key, manifest = resolve_manifest_for_doc(bucket_raw, manifest_prefix, doc_id)
    manifest_pages = manifest.get("pages", [])
    if not manifest_pages:
        raise ValueError(f"Manifest for doc_id='{doc_id}' has no pages")

    page_numbers = sorted(int(page.get("page_no", 0)) for page in manifest_pages)
    if page_filter is not None:
        page_numbers = [page_no for page_no in page_numbers if page_no in page_filter]
    if not page_numbers:
        raise ValueError(f"No pages to preprocess for doc_id='{doc_id}' with filter={pages!r}")

    outputs: list[str] = []
    for page_no in page_numbers:
        outputs.append(
            preprocess_single_page_from_split_minio(
                doc_id=doc_id,
                page_no=page_no,
                upload_minio=upload_minio,
            )
        )
    print(f"[opencv_preprocess] completed {len(outputs)} page(s) for doc_id='{doc_id}'")
    return outputs
