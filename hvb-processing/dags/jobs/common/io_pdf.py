from __future__ import annotations

from pathlib import Path


def list_pdf_files(raw_dir: str) -> list[Path]:
    # Discover input PDFs deterministically / Tìm file PDF đầu vào theo thứ tự ổn định
    return sorted(Path(raw_dir).glob("*.pdf"))
