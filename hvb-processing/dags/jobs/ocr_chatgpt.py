from __future__ import annotations

import base64
from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import run_with_timing
from common.ocr_prompt import HVB_OCR_PROMPT
from common.schema import OcrResult


def _recognize_chatgpt(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Call OpenAI vision API / Gọi OpenAI vision API
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'openai'.") from exc

    cfg = load_config()
    api_key = get_value(cfg, "openai", "api_key", fallback="")
    if not api_key:
        raise ValueError("Missing openai api_key (config or HVB_OPENAI_API_KEY)")

    model_name = get_value(cfg, "openai", "model", fallback="gpt-4o")
    client = OpenAI(api_key=api_key)
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": HVB_OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
        max_tokens=4096,
    )
    text = (response.choices[0].message.content or "").strip()
    return text, 0.0, []


def run(pdf_path: Path) -> list[OcrResult]:
    # ChatGPT OCR entrypoint / Điểm vào OCR ChatGPT
    return run_with_timing(pdf_path, model_name="chatgpt", recognize=_recognize_chatgpt)
