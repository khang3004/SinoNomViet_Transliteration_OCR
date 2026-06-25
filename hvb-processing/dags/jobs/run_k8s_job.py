#!/usr/bin/env python3
"""CLI entrypoint for HVB jobs running inside KubernetesPodOperator pods.

Điểm vào CLI cho job HVB chạy trong pod KubernetesPodOperator.
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


def _run_index_qdrant() -> None:
    from index_qdrant import index_pages_from_minio

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    model_folder = os.environ.get("HVB_MODEL_FOLDER", "paddle").strip() or "paddle"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    chunk_mode = os.environ.get("HVB_CHUNK_MODE", "page").strip() or "page"
    force_reindex = _as_bool(os.environ.get("HVB_FORCE_REINDEX"), default=False)
    index_pages_from_minio(
        doc_id=doc_id,
        model_folder=model_folder,
        pages=pages,
        chunk_mode=chunk_mode,
        force_reindex=force_reindex,
    )


def _run_ocr_paddle_pages() -> None:
    from ocr_runner import run_page_loop_from_split_minio

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    run_page_loop_from_split_minio(
        model="paddle",
        doc_id=doc_id,
        pages=pages,
        upload_minio=upload_minio,
        model_folder="paddle",
    )


def _run_ocr_gemini_pages() -> None:
    from ocr_runner import run_page_loop_from_split_minio

    doc_id = os.environ.get("HVB_DOC_ID", "hvb_base").strip() or "hvb_base"
    pages = _optional_pages(os.environ.get("HVB_PAGES"))
    upload_minio = _as_bool(os.environ.get("HVB_UPLOAD_MINIO"), default=True)
    run_page_loop_from_split_minio(
        model="gemini",
        doc_id=doc_id,
        pages=pages,
        upload_minio=upload_minio,
        model_folder="gemini",
    )


def main() -> None:
    # Dispatch HVB job by HVB_JOB env var / Điều phối job theo biến HVB_JOB
    os.environ.setdefault("HVB_CONFIG_PATH", "/workspace/hvb-processing/config.ini")
    os.environ.setdefault("HVB_PATHS_OUTPUT_DIR", "/tmp/hvb-output")
    os.environ.setdefault("HVB_SKIP_LOCAL_OUTPUT", "true")

    _ensure_jobs_on_path()
    job = os.environ.get("HVB_JOB", "").strip().lower()
    if job == "index_qdrant":
        _run_index_qdrant()
        return
    if job == "ocr_paddle_pages":
        _run_ocr_paddle_pages()
        return
    if job == "ocr_gemini_pages":
        _run_ocr_gemini_pages()
        return
    raise ValueError(f"Unsupported HVB_JOB '{job}'. Use: index_qdrant, ocr_paddle_pages, ocr_gemini_pages")


if __name__ == "__main__":
    main()
