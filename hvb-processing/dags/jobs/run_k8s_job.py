#!/usr/bin/env python3
"""CLI entrypoint for HVB v2 jobs running inside KubernetesPodOperator pods.

Điểm vào CLI cho job HVB v2 chạy trong pod KubernetesPodOperator.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _optional_pages(value: str | None) -> str | None:
    # Normalize optional pages filter / Chuẩn hóa filter trang; rỗng = tất cả
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: str | None, default: bool = False) -> bool:
    # Parse common truthy strings from env / Parse chuỗi boolean từ biến môi trường
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_jobs_on_path() -> None:
    # Ensure jobs package imports resolve in pod / Đảm bảo import jobs trong pod
    jobs_dir = Path(__file__).resolve().parent
    if str(jobs_dir) not in sys.path:
        sys.path.insert(0, str(jobs_dir))


def _run_opencv_preprocess_pages() -> None:
    from preprocess_opencv_runner import preprocess_page_loop_from_split_minio

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    preprocess_page_loop_from_split_minio(
        doc_id=doc_id,
        pages=pages,
        upload_minio=upload_minio,
    )


def _run_ocr_v2_pages() -> None:
    from ocr_v2_runner import ocr_page_loop_v2

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    page_kind = os.environ.get("HVB_PAGE_KIND", "toc").strip() or "toc"
    ocr_page_loop_v2(
        doc_id=doc_id,
        pages=pages,
        upload_minio=upload_minio,
        page_kind=page_kind,
    )


def _run_align_v2_pages() -> None:
    from align_v2_runner import align_page_loop_v2

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    align_page_loop_v2(doc_id=doc_id, pages=pages, upload_minio=upload_minio)


def _run_build_catalog() -> None:
    from build_catalog_runner import build_catalog_from_ocr

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    build_catalog_from_ocr(doc_id=doc_id, pages=pages, upload_minio=upload_minio)


def _run_refine_entries() -> None:
    from refine_entries_runner import refine_catalog_entries

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    force_all = _as_bool(os.environ.get("HVB_FORCE"), default=False)
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    refine_catalog_entries(
        doc_id=doc_id,
        pages=pages,
        force_all=force_all,
        upload_minio=upload_minio,
    )


def _run_stitch_entries() -> None:
    from stitch_entries_runner import stitch_incomplete_entries

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    stitch_incomplete_entries(doc_id=doc_id, pages=pages, upload_minio=upload_minio)


def _run_index_pairs_qdrant() -> None:
    from index_pairs_qdrant import index_aligned_pairs_from_minio

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    index_aligned_pairs_from_minio(doc_id=doc_id, pages=pages)


def _run_index_catalog_qdrant() -> None:
    from index_catalog_qdrant import index_catalog_entries_to_qdrant

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    recreate = _as_bool(os.environ.get("HVB_QDRANT_RECREATE"), default=False)
    index_catalog_entries_to_qdrant(doc_id=doc_id, pages=pages, recreate=recreate)


def _run_cleanup_toc_state() -> None:
    from cleanup_toc_state import cleanup_toc_migration_state

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES")) or "49-58"
    recreate = _as_bool(os.environ.get("HVB_QDRANT_RECREATE"), default=True)
    cleanup_toc_migration_state(doc_id=doc_id, pages=pages, recreate_qdrant=recreate)


def main() -> None:
    # Dispatch HVB v2 job by HVB_JOB env var / Điều phối job v2 theo biến HVB_JOB
    os.environ.setdefault("HVB_CONFIG_PATH", "/workspace/hvb-processing/config.ini")
    os.environ.setdefault("HVB_PATHS_OUTPUT_DIR", "/tmp/hvb-output")
    os.environ.setdefault("HVB_SKIP_LOCAL_OUTPUT", "true")

    _ensure_jobs_on_path()
    job = os.environ.get("HVB_JOB", "").strip().lower()
    if job == "opencv_preprocess_pages":
        _run_opencv_preprocess_pages()
        return
    if job == "ocr_v2_pages":
        _run_ocr_v2_pages()
        return
    if job == "align_v2_pages":
        _run_align_v2_pages()
        return
    if job == "build_catalog":
        _run_build_catalog()
        return
    if job == "refine_entries":
        _run_refine_entries()
        return
    if job == "stitch_entries":
        _run_stitch_entries()
        return
    if job == "index_pairs_qdrant":
        _run_index_pairs_qdrant()
        return
    if job == "index_catalog_qdrant":
        _run_index_catalog_qdrant()
        return
    if job == "cleanup_toc_state":
        _run_cleanup_toc_state()
        return
    raise ValueError(
        f"Unsupported HVB_JOB '{job}'. "
        "Use: opencv_preprocess_pages, ocr_v2_pages, align_v2_pages, "
        "build_catalog, refine_entries, stitch_entries, "
        "index_pairs_qdrant, index_catalog_qdrant, cleanup_toc_state"
    )


if __name__ == "__main__":
    main()
