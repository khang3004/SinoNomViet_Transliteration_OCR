from __future__ import annotations

import os
from pathlib import Path

# Default render DPI for OCR / DPI mặc định khi render PDF sang ảnh
DEFAULT_DPI = 300


def get_render_dpi() -> int:
    # Resolve PDF render DPI from env or config / Lấy DPI render PDF từ env hoặc config
    env_dpi = os.environ.get("HVB_OPENCV_PREPROCESS_RENDER_DPI", "").strip() or os.environ.get(
        "HVB_PADDLE_RENDER_DPI", ""
    ).strip()
    if env_dpi:
        return int(env_dpi)
    try:
        from common.config import get_value, load_config

        return int(
            get_value(
                load_config(),
                "opencv_preprocess",
                "render_dpi",
                fallback=str(DEFAULT_DPI),
            )
        )
    except Exception:
        return DEFAULT_DPI


def pdf_to_png_bytes(pdf_path: Path, dpi: int | None = None) -> bytes:
    # Convert first page of PDF to PNG bytes / Chuyển trang đầu PDF thành bytes PNG
    try:
        import fitz  # pymupdf
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'pymupdf'. Install dags/requirements.txt in Airflow.") from exc

    render_dpi = dpi if dpi is not None else get_render_dpi()
    document = fitz.open(pdf_path)
    try:
        if document.page_count < 1:
            raise ValueError(f"PDF has no pages: {pdf_path}")
        page = document.load_page(0)
        scale = render_dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")
    finally:
        document.close()
