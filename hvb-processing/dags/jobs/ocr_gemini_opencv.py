from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import resize_png_bytes
from common.ocr_confidence import estimate_gemini_ocr_confidence
from common.ocr_prompt import (
    HVB_OCR_STRUCTURED_PROMPT,
    HVB_OCR_TOC_BOTTOM_PROMPT,
    HVB_OCR_TOC_PROMPT,
)

# Ramclouds vision models (check dashboard; 2.5/3-pro often unavailable) / Model vision Ramclouds
GEMINI_OPENCV_FALLBACK_MODELS = (
    "gemini-3.5-flash-low",
)


def _parse_retry_delay_seconds(message: str) -> float | None:
    # Parse API-suggested wait from 429 body / Đọc thời gian chờ từ thông báo 429
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
    # Switch model on missing channel / unavailable model / Đổi model khi không có channel hoặc model lỗi
    message = str(exc).lower()
    if _is_rate_limit(exc):
        return False
    if "model_not_found" in message:
        return True
    if "no available channel" in message:
        return True
    if "404" in message and ("not found" in message or "is not found" in message):
        return True
    if "not supported" in message:
        return True
    return False


def _recognize_openai_compatible(
    image_bytes: bytes,
    *,
    prompt: str = HVB_OCR_STRUCTURED_PROMPT,
) -> tuple[str, float, list[dict]]:
    # Vision OCR via OpenAI-compatible API (Ramclouds, etc.) / OCR vision qua API tương thích OpenAI
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'openai'.") from exc

    cfg = load_config()
    api_key = get_value(cfg, "gemini_opencv", "api_key", fallback="")
    if not api_key:
        raise ValueError("Missing gemini_opencv api_key (config or HVB_GEMINI_OPENCV_API_KEY)")

    base_url = get_value(cfg, "gemini_opencv", "base_url", fallback="https://ramclouds.me/v1").strip()
    primary_model = get_value(
        cfg,
        "gemini_opencv",
        "model",
        fallback="gemini-3.5-flash-low",
    )
    max_retries = int(get_value(cfg, "gemini_opencv", "max_retries", fallback="6"))
    max_image_side = int(get_value(cfg, "gemini_opencv", "max_image_side", fallback="1536"))
    # TOC JSON needs more tokens for multi-entry pages / TOC nhiều entry cần thêm token
    max_tokens = int(get_value(cfg, "gemini_opencv", "max_tokens", fallback="8192"))
    if "muc_luc" in prompt.lower() or "MỤC LỤC" in prompt or "TRÍCH YẾU" in prompt:
        max_tokens = max(max_tokens, 8192)

    model_candidates: list[str] = []
    for name in [primary_model, *GEMINI_OPENCV_FALLBACK_MODELS]:
        if name not in model_candidates:
            model_candidates.append(name)

    image_bytes = resize_png_bytes(image_bytes, max_side=max_image_side)
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    client_kwargs: dict = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    last_error: Exception | None = None
    for model_name in model_candidates:
        print(f"[gemini_opencv] trying model={model_name}")
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                                },
                            ],
                        }
                    ],
                    max_tokens=max_tokens,
                )
                text = (response.choices[0].message.content or "").strip()
                confidence = estimate_gemini_ocr_confidence(text)
                return text, confidence, []
            except Exception as exc:
                last_error = exc
                if _is_rate_limit(exc) and attempt < max_retries - 1:
                    delay = _parse_retry_delay_seconds(str(exc)) or (15.0 * (attempt + 1))
                    time.sleep(min(delay + 1.0, 120.0))
                    continue
                if _should_try_next_model(exc):
                    print(f"[gemini_opencv] model={model_name} unavailable: {exc}")
                    break
                raise RuntimeError(str(exc)) from exc

    if last_error:
        raise RuntimeError(str(last_error))
    raise RuntimeError("Gemini OpenCV OCR failed for all configured models")


def _recognize_native_gemini(
    image_bytes: bytes,
    *,
    prompt: str = HVB_OCR_STRUCTURED_PROMPT,
) -> tuple[str, float, list[dict]]:
    # Native Google Gemini SDK when key is not OpenAI-style / SDK Google khi key không phải sk-
    try:
        import google.generativeai as genai
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'google-generativeai' or 'Pillow'.") from exc
    import io

    cfg = load_config()
    api_key = get_value(cfg, "gemini_opencv", "api_key", fallback="")
    if not api_key:
        raise ValueError("Missing gemini_opencv api_key")

    model_name = get_value(cfg, "gemini_opencv", "model", fallback="gemini-2.0-flash-lite")
    max_image_side = int(get_value(cfg, "gemini_opencv", "max_image_side", fallback="1536"))
    max_retries = int(get_value(cfg, "gemini_opencv", "max_retries", fallback="6"))

    image_bytes = resize_png_bytes(image_bytes, max_side=max_image_side)
    image = Image.open(io.BytesIO(image_bytes))
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = model.generate_content([prompt, image])
            text = (response.text or "").strip()
            confidence = estimate_gemini_ocr_confidence(text)
            return text, confidence, []
        except Exception as exc:
            last_error = exc
            if _is_rate_limit(exc) and attempt < max_retries - 1:
                delay = _parse_retry_delay_seconds(str(exc)) or (15.0 * (attempt + 1))
                time.sleep(min(delay + 1.0, 120.0))
                continue
            raise RuntimeError(str(exc)) from exc

    if last_error:
        raise RuntimeError(str(last_error))
    raise RuntimeError("Native Gemini OCR failed")


def recognize_gemini_opencv(
    image_bytes: bytes,
    *,
    page_kind: str = "body",
    prompt_override: str | None = None,
) -> tuple[str, float, list[dict]]:
    # Route API + TOC/body prompt by page_kind / Chọn API + prompt TOC/body theo page_kind
    kind = (page_kind or "body").strip().lower()
    if prompt_override:
        prompt = prompt_override
    elif kind in {"toc", "muc_luc", "catalog"}:
        prompt = HVB_OCR_TOC_PROMPT
    else:
        prompt = HVB_OCR_STRUCTURED_PROMPT
    cfg = load_config()
    api_key = get_value(cfg, "gemini_opencv", "api_key", fallback="")
    base_url = get_value(cfg, "gemini_opencv", "base_url", fallback="").strip()
    if base_url or str(api_key).startswith("sk-"):
        return _recognize_openai_compatible(image_bytes, prompt=prompt)
    return _recognize_native_gemini(image_bytes, prompt=prompt)


def crop_png_bottom(image_bytes: bytes, *, keep_ratio: float = 0.45) -> bytes:
    """Keep bottom portion of PNG for last-entry OCR retry.

    Giữ phần dưới PNG để OCR lại mục cuối trang.
    """
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes
        height = img.shape[0]
        top = max(0, int(height * (1.0 - keep_ratio)))
        cropped = img[top:height, :]
        ok, encoded = cv2.imencode(".png", cropped)
        if not ok:
            return image_bytes
        return encoded.tobytes()
    except ModuleNotFoundError:
        # Fallback when OpenCV missing locally / Fallback khi thiếu OpenCV trên máy local
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        top = max(0, int(height * (1.0 - keep_ratio)))
        cropped = img.crop((0, top, width, height))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()


def recognize_toc_bottom_retry(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Second-pass OCR on page bottom / OCR lần 2 trên phần đuôi trang
    bottom = crop_png_bottom(image_bytes, keep_ratio=0.5)
    return recognize_gemini_opencv(
        bottom,
        page_kind="toc",
        prompt_override=HVB_OCR_TOC_BOTTOM_PROMPT,
    )


def run_from_png_path(
    png_path: Path,
    *,
    page_kind: str = "body",
) -> tuple[str, float, list[dict]]:
    # OCR one local denoised PNG / OCR một file PNG đã lọc nhiễu
    return recognize_gemini_opencv(png_path.read_bytes(), page_kind=page_kind)
