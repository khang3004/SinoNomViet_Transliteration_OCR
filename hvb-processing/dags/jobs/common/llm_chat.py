from __future__ import annotations

import time

from common.config import get_value, load_config


def _resolve_api_key() -> str:
    # Prefer align key, fallback OCR key / Ưu tiên key align, fallback key OCR
    cfg = load_config()
    api_key = get_value(cfg, "align", "api_key", fallback="").strip()
    if not api_key:
        api_key = get_value(cfg, "gemini_opencv", "api_key", fallback="").strip()
    if not api_key:
        raise ValueError("Missing align/gemini api_key for chat completion")
    return api_key


def call_chat_completion(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> tuple[str, str]:
    """Call OpenAI-compatible chat; return (text, model_used).

    Gọi chat tương thích OpenAI; trả về (text, model_đã_dùng).
    """
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'openai'.") from exc

    cfg = load_config()
    base_url = get_value(cfg, "align", "base_url", fallback="https://ramclouds.me/v1").strip()
    primary = model or get_value(cfg, "align", "model", fallback="deepseek-v4-flash")
    fallback = get_value(cfg, "align", "fallback_model", fallback="gemini-3.5-flash-low")
    max_retries = int(get_value(cfg, "align", "max_retries", fallback="4"))
    client = OpenAI(api_key=_resolve_api_key(), base_url=base_url)

    candidates = [primary]
    if fallback and fallback not in candidates:
        candidates.append(fallback)

    last_error: Exception | None = None
    for model_name in candidates:
        for attempt in range(max_retries):
            try:
                print(f"[llm_chat] trying model={model_name}")
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                text = (response.choices[0].message.content or "").strip()
                return text, model_name
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if "429" in message and attempt < max_retries - 1:
                    time.sleep(10.0 * (attempt + 1))
                    continue
                if "model_not_found" in message or "no available channel" in message or "404" in message:
                    print(f"[llm_chat] model unavailable: {model_name} ({exc})")
                    break
                raise
    if last_error:
        raise RuntimeError(str(last_error))
    raise RuntimeError("chat completion failed")
