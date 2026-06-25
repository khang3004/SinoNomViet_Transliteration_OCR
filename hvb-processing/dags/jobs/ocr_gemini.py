from __future__ import annotations

import io
import re
import time
from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import resize_png_bytes, run_with_timing
from common.ocr_prompt import HVB_OCR_PROMPT
from common.schema import OcrResult

# Fallback only when primary model name is invalid / Chỉ dự phòng khi tên model chính không tồn tại
GEMINI_FALLBACK_MODELS = (
    "gemini-2.0-flash",
)


def _parse_retry_delay_seconds(message: str) -> float | None:
    # Parse API-suggested wait from 429 body / Đọc thời gian chờ từ thông báo 429 của API
    match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", message, flags=re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"seconds:\s*(\d+)", message)
    if match:
        return float(match.group(1))
    return None


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc)


def _should_try_next_model(exc: Exception) -> bool:
    # Switch model only on unknown name — never on quota / Chỉ đổi model khi tên sai, không đổi khi hết quota
    message = str(exc).lower()
    if _is_rate_limit(exc):
        return False
    if "404" in message and ("not found" in message or "is not found" in message):
        return True
    if "not supported for generatecontent" in message:
        return True
    return False


def _quota_hint(exc: Exception) -> str:
    # Append billing hint when free tier is exhausted / Gợi ý bật billing khi hết quota free tier
    message = str(exc)
    if "quota" in message.lower() or "limit: 0" in message.lower():
        return (
            f"{message}\n\n"
            "Gemini free-tier quota may be exhausted. Options: wait for daily reset, "
            "enable billing at https://aistudio.google.com/apikey, or reduce pages per run."
        )
    return message


def _generate_with_retry(model, image, max_retries: int = 6):
    # Retry same model on 429 with API backoff / Thử lại cùng model khi 429 với backoff từ API
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return model.generate_content([HVB_OCR_PROMPT, image])
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if not _is_rate_limit(exc) or attempt >= max_retries - 1:
                raise
            delay = _parse_retry_delay_seconds(message) or (15.0 * (attempt + 1))
            time.sleep(min(delay + 1.0, 120.0))
    if last_error:
        raise last_error
    raise RuntimeError("Gemini generate_content failed without exception")


def _recognize_gemini(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Call Gemini vision API / Gọi Gemini vision API
    try:
        import google.generativeai as genai
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'google-generativeai'.") from exc

    cfg = load_config()
    api_key = get_value(cfg, "gemini", "api_key", fallback="")
    if not api_key:
        raise ValueError("Missing gemini api_key (config or HVB_GEMINI_API_KEY)")

    primary_model = get_value(cfg, "gemini", "model", fallback="gemini-2.5-flash")
    max_retries = int(get_value(cfg, "gemini", "max_retries", fallback="6"))
    max_image_side = int(get_value(cfg, "gemini", "max_image_side", fallback="1536"))

    model_candidates: list[str] = []
    for name in [primary_model, *GEMINI_FALLBACK_MODELS]:
        if name not in model_candidates:
            model_candidates.append(name)

    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'Pillow'.") from exc

    # Shrink page image to reduce input tokens / Thu nhỏ ảnh trang để giảm input token
    image_bytes = resize_png_bytes(image_bytes, max_side=max_image_side)
    image = Image.open(io.BytesIO(image_bytes))
    genai.configure(api_key=api_key)

    last_error: Exception | None = None
    for model_name in model_candidates:
        try:
            model = genai.GenerativeModel(model_name)
            response = _generate_with_retry(model, image, max_retries=max_retries)
            text = (response.text or "").strip()
            return text, 0.0, []
        except Exception as exc:
            last_error = exc
            if _should_try_next_model(exc):
                continue
            raise RuntimeError(_quota_hint(exc)) from exc

    if last_error:
        raise RuntimeError(_quota_hint(last_error))
    raise RuntimeError("Gemini OCR failed for all configured models")


def run(pdf_path: Path) -> list[OcrResult]:
    # Gemini OCR entrypoint / Điểm vào OCR Gemini
    return run_with_timing(pdf_path, model_name="gemini", recognize=_recognize_gemini)
