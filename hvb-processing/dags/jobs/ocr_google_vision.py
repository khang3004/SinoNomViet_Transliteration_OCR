from __future__ import annotations

import os
from pathlib import Path

from common.config import get_value, load_config
from common.ocr_helpers import run_with_timing
from common.schema import OcrResult


def _recognize_google_vision(image_bytes: bytes) -> tuple[str, float, list[dict]]:
    # Call Google Cloud Vision document OCR / Gọi Google Cloud Vision OCR tài liệu
    try:
        from google.cloud import vision
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'google-cloud-vision'.") from exc

    cfg = load_config()
    credentials_path = get_value(cfg, "google_vision", "credentials_json", fallback="")
    if credentials_path and os.path.isfile(credentials_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise ValueError(
            "Missing Google credentials. Set google_vision.credentials_json or GOOGLE_APPLICATION_CREDENTIALS."
        )

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(response.error.message)

    annotation = response.full_text_annotation
    text = (annotation.text if annotation else "").strip()

    blocks: list[dict] = []
    if annotation:
        for page in annotation.pages:
            for block in page.blocks:
                block_text_parts: list[str] = []
                for paragraph in block.paragraphs:
                    for word in paragraph.words:
                        word_text = "".join(symbol.text for symbol in word.symbols)
                        block_text_parts.append(word_text)
                blocks.append(
                    {
                        "text": " ".join(block_text_parts).strip(),
                        "confidence": getattr(block, "confidence", 0.0) or 0.0,
                    }
                )

    avg_confidence = 0.0
    if blocks:
        avg_confidence = sum(item.get("confidence", 0.0) for item in blocks) / len(blocks)

    return text, avg_confidence, blocks


def run(pdf_path: Path) -> list[OcrResult]:
    # Google Vision OCR entrypoint / Điểm vào OCR Google Vision
    return run_with_timing(pdf_path, model_name="google_vision", recognize=_recognize_google_vision)
