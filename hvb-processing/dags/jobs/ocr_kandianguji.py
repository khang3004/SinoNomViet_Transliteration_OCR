from __future__ import annotations

from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import post_json_ocr, run_with_timing
from common.schema import OcrResult


def _recognize_kandianguji(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Call KanDianGuJi-compatible HTTP OCR API / Gọi API OCR tương thích KanDianGuJi
    cfg = load_config()
    service_url = get_value(cfg, "kandianguji", "service_url", fallback="")
    api_key = get_value(cfg, "kandianguji", "api_key", fallback="")
    if not service_url:
        raise ValueError("Missing kandianguji.service_url (config or HVB_KANDIANGUJI_SERVICE_URL)")
    return post_json_ocr(service_url=service_url, image_bytes=image_bytes, api_key=api_key or None)


def run(pdf_path: Path) -> list[OcrResult]:
    # KanDianGuJi OCR entrypoint / Điểm vào OCR KanDianGuJi
    return run_with_timing(pdf_path, model_name="kandianguji", recognize=_recognize_kandianguji)
