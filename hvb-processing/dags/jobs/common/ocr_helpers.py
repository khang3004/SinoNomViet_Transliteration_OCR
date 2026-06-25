from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Callable

from common.schema import OcrResult

# Max PNG side sent to Paddle service / Cạnh dài tối đa của PNG gửi sang Paddle
PADDLE_MAX_IMAGE_SIDE = 2048


def resize_png_bytes(image_bytes: bytes, max_side: int = PADDLE_MAX_IMAGE_SIDE) -> bytes:
    # Downscale large page renders to reduce service memory / Thu nhỏ ảnh trang lớn để giảm RAM service
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'Pillow'. Install dags/requirements.txt.") from exc

    with Image.open(BytesIO(image_bytes)) as image:
        width, height = image.size
        longest = max(width, height)
        if longest <= max_side:
            return image_bytes
        scale = max_side / float(longest)
        resized = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
        output = BytesIO()
        resized.save(output, format="PNG", optimize=True)
        return output.getvalue()


def run_with_timing(
    pdf_path: Path,
    model_name: str,
    recognize: Callable[[bytes], tuple[str, float, list[dict]]],
) -> list[OcrResult]:
    # Run OCR adapter with unified error handling / Chạy adapter OCR với xử lý lỗi thống nhất
    from common.preprocess import pdf_to_png_bytes

    start = perf_counter()
    try:
        image_bytes = pdf_to_png_bytes(pdf_path)
        text, confidence, blocks = recognize(image_bytes)
        elapsed_ms = int((perf_counter() - start) * 1000)
        return [
            OcrResult(
                doc_id=pdf_path.stem,
                source_pdf=str(pdf_path),
                model_name=model_name,
                page_no=1,
                text=text,
                confidence=confidence,
                blocks=blocks,
                elapsed_ms=elapsed_ms,
            )
        ]
    except Exception as exc:
        elapsed_ms = int((perf_counter() - start) * 1000)
        return [
            OcrResult(
                doc_id=pdf_path.stem,
                source_pdf=str(pdf_path),
                model_name=model_name,
                page_no=1,
                text="",
                confidence=0.0,
                blocks=[],
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )
        ]


def post_json_ocr(
    service_url: str,
    image_bytes: bytes,
    api_key: str | None = None,
    timeout_sec: int = 120,
    retries: int = 3,
    retry_delay_sec: float = 10.0,
) -> tuple[str, float, list[dict]]:
    # Call internal OCR HTTP service / Gọi service OCR nội bộ qua HTTP
    import base64
    import json

    try:
        import requests
        from requests.exceptions import ConnectionError as RequestsConnectionError
        from requests.exceptions import ChunkedEncodingError, Timeout
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'requests'. Install dags/requirements.txt.") from exc

    endpoint = service_url.rstrip("/") + "/ocr"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"image_base64": base64.standard_b64encode(image_bytes).decode("ascii")}
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_sec)
            response.raise_for_status()
            data = response.json()
            text = str(data.get("text", ""))
            confidence = float(data.get("confidence", 0.0))
            blocks = data.get("blocks", [])
            if not isinstance(blocks, list):
                blocks = []
            return text, confidence, blocks
        except (RequestsConnectionError, ChunkedEncodingError, Timeout) as exc:
            last_error = exc
            if attempt + 1 >= retries:
                break
            time.sleep(retry_delay_sec * (attempt + 1))

    if last_error is not None:
        raise last_error
    raise RuntimeError("OCR HTTP request failed without response")
