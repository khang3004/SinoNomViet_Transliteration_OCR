from __future__ import annotations

from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import post_json_ocr, resize_png_bytes, run_with_timing
from common.schema import OcrResult
from common.vi_metadata_fix import apply_vi_metadata_fix


def _metadata_fix_enabled(cfg) -> bool:
    # Toggle dictionary fix from config / Bật/tắt sửa từ điển metadata qua config
    raw = get_value(cfg, "paddle", "metadata_fix_enabled", fallback="true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _recognize_paddle(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Call PaddleOCR microservice on K3s / Gọi microservice PaddleOCR trên K3s
    cfg = load_config()
    service_url = get_value(
        cfg,
        "paddle",
        "service_url",
        fallback="http://hvb-paddle-ocr.ocr.svc.cluster.local:8080",
    )
    if not service_url:
        raise ValueError("Missing paddle.service_url (config or HVB_PADDLE_SERVICE_URL)")
    max_side = int(get_value(cfg, "paddle", "max_image_side", fallback="2560"))
    image_bytes = resize_png_bytes(image_bytes, max_side=max_side)
    text, confidence, blocks = post_json_ocr(
        service_url=service_url,
        image_bytes=image_bytes,
        timeout_sec=300,
        retries=4,
        retry_delay_sec=15.0,
    )
    if _metadata_fix_enabled(cfg):
        text, blocks = apply_vi_metadata_fix(text, blocks)
    return text, confidence, blocks


def run(pdf_path: Path) -> list[OcrResult]:
    # PaddleOCR entrypoint via HTTP service / Điểm vào PaddleOCR qua HTTP service
    return run_with_timing(pdf_path, model_name="paddleocr", recognize=_recognize_paddle)
