from __future__ import annotations

from typing import Any

from common.config import get_value, load_config
from common.schema import OcrResult

_REFINE_PROMPT = """You correct OCR text from Vietnamese metadata pages of Nguyen-dynasty archival manuscripts (Chau ban).

Input is noisy Paddle OCR. Rules:
1. Fix Vietnamese diacritics and spacing (e.g. CUC VAN THU -> CỤC VĂN THƯ VÀ LƯU TRỮ NHÀ NƯỚC).
2. Preserve Chinese characters exactly as in the input.
3. Preserve line breaks.
4. Do not invent content. Output only the corrected text.

OCR input:
{text}
"""


def _ollama_enabled(cfg) -> bool:
    # Check config/env toggle / Kiểm tra bật/tắt Ollama refine
    raw = get_value(cfg, "ollama", "enabled", fallback="false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _ollama_settings(cfg) -> tuple[str, str, int, float]:
    # Resolve Ollama endpoint and model / Lấy endpoint và model Ollama
    base_url = get_value(
        cfg,
        "ollama",
        "base_url",
        fallback="http://ollama.llm.svc.cluster.local:11434",
    ).rstrip("/")
    model = get_value(cfg, "ollama", "model", fallback="qwen2.5:3b")
    timeout_sec = int(get_value(cfg, "ollama", "timeout_sec", fallback="120"))
    temperature = float(get_value(cfg, "ollama", "temperature", fallback="0.1"))
    return base_url, model, timeout_sec, temperature


def refine_metadata_text(text: str, cfg=None) -> str:
    # Call Ollama chat API to fix metadata Latin/Vietnamese / Gọi Ollama sửa metadata Latin/Việt
    if not text.strip():
        return text

    cfg = cfg or load_config()
    base_url, model, timeout_sec, temperature = _ollama_settings(cfg)

    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'requests'.") from exc

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _REFINE_PROMPT.format(text=text)}],
        "stream": False,
        "options": {"temperature": temperature},
    }
    response = requests.post(
        f"{base_url}/api/chat",
        json=payload,
        timeout=timeout_sec,
    )
    response.raise_for_status()
    data = response.json()
    message = data.get("message", {})
    refined = str(message.get("content", "")).strip()
    if not refined:
        raise RuntimeError("Ollama returned empty refinement")
    return refined


def maybe_refine_paddle_metadata(result: OcrResult, cfg=None) -> OcrResult:
    # Refine early metadata pages after Paddle; fail open / Sửa trang metadata; lỗi thì giữ Paddle
    cfg = cfg or load_config()
    if not _ollama_enabled(cfg):
        return result
    if result.error:
        return result

    max_page = int(get_value(cfg, "ollama", "metadata_max_page", fallback="10"))
    if result.page_no > max_page:
        return result

    try:
        refined = refine_metadata_text(result.text, cfg=cfg)
        if refined.strip():
            result.text = refined.strip()
    except Exception as exc:
        print(f"[ollama] refine skipped for page {result.page_no}: {exc}")
    return result
