from __future__ import annotations

import time

from common.config import get_value, load_config

_ALIGN_PROMPT = """Bạn là chuyên gia dóng hàng văn bản Châu bản triều Nguyễn (Hán Nôm ↔ Quốc ngữ).
Cho JSON OCR trang sách (metadata + blocks). Hãy tạo các cặp song ngữ theo đúng nội dung.

Trả về ĐÚNG một JSON (không markdown):
{{
  "content_alignment": [
    {{"han_nom": "...", "quoc_ngu": "..."}},
    {{"han_nom": "...", "quoc_ngu": "..."}}
  ]
}}

Quy tắc:
- Mỗi phần tử là một cặp nghĩa tương ứng.
- Nếu chỉ có Hán Nôm: điền han_nom, quoc_ngu để chuỗi rỗng (hoặc dịch ngắn nếu block đã có bản Việt gần đó).
- Nếu chỉ có Quốc ngữ: điền quoc_ngu, han_nom để chuỗi rỗng.
- Giữ thứ tự văn bản trên trang.
- Không bịa nội dung ngoài nguồn OCR.

OCR JSON:
{ocr_json}
"""


def _align_api_key() -> str:
    # Resolve Ramclouds API key for align / Lấy API key Ramclouds cho bước align
    cfg = load_config()
    api_key = get_value(cfg, "align", "api_key", fallback="").strip()
    if not api_key:
        api_key = get_value(cfg, "gemini_opencv", "api_key", fallback="").strip()
    if not api_key:
        raise ValueError("Missing align api_key (HVB_ALIGN_API_KEY or HVB_GEMINI_OPENCV_API_KEY)")
    return api_key


def _call_chat(model: str, prompt: str) -> str:
    # Call OpenAI-compatible chat completion / Gọi chat completion tương thích OpenAI
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'openai'.") from exc

    cfg = load_config()
    base_url = get_value(cfg, "align", "base_url", fallback="https://ramclouds.me/v1").strip()
    max_retries = int(get_value(cfg, "align", "max_retries", fallback="4"))
    client = OpenAI(api_key=_align_api_key(), base_url=base_url)
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            print(f"[align] trying model={model}")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
                temperature=0.1,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if "429" in message and attempt < max_retries - 1:
                time.sleep(10.0 * (attempt + 1))
                continue
            raise
    if last_error:
        raise RuntimeError(str(last_error))
    raise RuntimeError("Align API call failed")


def align_ocr_page_with_deepseek(ocr_payload: dict) -> tuple[list[dict[str, str]], str]:
    # Align bilingual pairs; fallback to Gemini if DeepSeek fails / Dóng hàng cặp song ngữ; fallback Gemini nếu DeepSeek lỗi
    import json

    cfg = load_config()
    primary = get_value(cfg, "align", "model", fallback="deepseek-v4-flash")
    fallback = get_value(cfg, "align", "fallback_model", fallback="gemini-3.5-flash-low")
    # Compact OCR sent to align model / OCR gọn gửi sang model align
    compact = {
        "ngay_thang": ocr_payload.get("ngay_thang"),
        "the_loai": ocr_payload.get("the_loai"),
        "de_tai": ocr_payload.get("de_tai"),
        "blocks": ocr_payload.get("blocks") or [],
        "raw_text": (ocr_payload.get("raw_text") or "")[:8000],
    }
    prompt = _ALIGN_PROMPT.format(ocr_json=json.dumps(compact, ensure_ascii=False))

    models = [primary]
    if fallback and fallback not in models:
        models.append(fallback)

    last_error: Exception | None = None
    for model in models:
        try:
            raw = _call_chat(model, prompt)
            from common.chau_ban_schema import parse_align_response

            pairs = parse_align_response(raw)
            if pairs:
                return pairs, model
            # Empty parse → try next model / Parse rỗng → thử model kế
            last_error = RuntimeError(f"Empty alignment from model={model}")
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if "model_not_found" in message or "no available channel" in message or "404" in message:
                print(f"[align] model unavailable: {model} ({exc})")
                continue
            raise
    if last_error:
        raise RuntimeError(str(last_error))
    return [], primary
