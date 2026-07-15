from __future__ import annotations

"""One-shot cleanup for TOC/STT migration: drop stale page OCR/aligned + Qdrant.

Dọn một lần khi migrate TOC/STT: xóa OCR/aligned cũ theo trang và Qdrant cũ.
"""

from common.config import get_value, load_config
from common.io_storage import delete_objects_with_prefix
from common.page_utils import parse_pages_filter
from common.qdrant_schema import ensure_hvb_chau_ban_pairs_collection


def cleanup_toc_migration_state(
    doc_id: str,
    *,
    pages: str | list[int] | None = "49-58",
    recreate_qdrant: bool = True,
) -> dict[str, int]:
    cfg = load_config()
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    bucket_aligned = get_value(cfg, "minio", "bucket_aligned", fallback="hvb-aligned")
    page_filter = parse_pages_filter(pages)

    deleted_ocr = 0
    deleted_aligned = 0
    # Delete selected page JSON under doc prefix / Xóa JSON trang chọn trong prefix doc
    from common.io_storage import list_objects_with_prefix
    from pathlib import Path

    for bucket, counter_name in ((bucket_ocr, "ocr"), (bucket_aligned, "aligned")):
        keys = [
            key
            for key in list_objects_with_prefix(bucket=bucket, prefix=f"{doc_id}/", suffix=".json")
            if Path(key).name.startswith("page_")
        ]
        for key in keys:
            page_no = int(Path(key).stem.split("_")[1])
            if page_filter is not None and page_no not in page_filter:
                continue
            from common.io_storage import delete_object

            if delete_object(bucket, key):
                if counter_name == "ocr":
                    deleted_ocr += 1
                else:
                    deleted_aligned += 1
                print(f"[cleanup] deleted {bucket}/{key}")

    if recreate_qdrant:
        name = ensure_hvb_chau_ban_pairs_collection(recreate=True)
        print(f"[cleanup] qdrant recreated: {name}")

    # Ensure new buckets exist via empty list call / Đảm bảo bucket mới tồn tại
    bucket_catalog = get_value(cfg, "minio", "bucket_catalog", fallback="hvb-catalog")
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    list_objects_with_prefix(bucket=bucket_catalog, prefix=f"{doc_id}/")
    list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/")

    # Clear old catalog/entries for doc when remigrating / Xóa catalog/entries cũ khi migrate lại
    deleted_catalog = delete_objects_with_prefix(bucket_catalog, f"{doc_id}/", suffix=".json")
    deleted_entries = delete_objects_with_prefix(bucket_entries, f"{doc_id}/", suffix=".json")

    result = {
        "deleted_ocr": deleted_ocr,
        "deleted_aligned": deleted_aligned,
        "deleted_catalog": deleted_catalog,
        "deleted_entries": deleted_entries,
    }
    print(f"[cleanup] done: {result}")
    return result
