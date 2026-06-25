from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from block_merge import merge_dual_pass_blocks
from text_layout import reconstruct_text_from_blocks

_ocr_engine_ch: Any | None = None
_ocr_engine_latin: Any | None = None
_resolved_device: str | None = None
_ocr_profile: dict[str, str] = {}
_latin_profile: dict[str, str] = {}
# Cap longest image side before inference to reduce memory / Giới hạn cạnh dài nhất trước inference để giảm RAM
MAX_IMAGE_SIDE = int(os.getenv("PADDLE_MAX_IMAGE_SIDE", "2048"))

# Model tier: server (PP-OCRv5) or mobile (PP-OCRv4) / Bậc model: server hoặc mobile
OCR_TIER = os.getenv("PADDLE_OCR_TIER", "server").strip().lower()
LATIN_LANG = os.getenv("PADDLE_LATIN_LANG", "vi").strip() or "vi"


def _latin_enabled() -> bool:
    # Toggle dual-pass Latin OCR / Bật/tắt pass Latin song song
    raw = os.getenv("PADDLE_LATIN_ENABLED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return OCR_TIER == "server"


def _resolve_ocr_models() -> dict[str, str]:
    # Pick detection/recognition models from tier env / Chọn model det/rec theo PADDLE_OCR_TIER
    if OCR_TIER == "mobile":
        return {
            "ocr_version": "PP-OCRv4",
            "text_detection_model_name": "PP-OCRv4_mobile_det",
            "text_recognition_model_name": "PP-OCRv4_mobile_rec",
        }
    return {
        "ocr_version": "PP-OCRv5",
        "text_detection_model_name": "PP-OCRv5_server_det",
        "text_recognition_model_name": "PP-OCRv5_server_rec",
    }


def _resolve_latin_models() -> dict[str, str]:
    # Latin pass uses server det + latin mobile rec / Pass Latin: det server + rec latin mobile
    if OCR_TIER == "mobile":
        return {
            "ocr_version": "PP-OCRv5",
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "latin_PP-OCRv5_mobile_rec",
        }
    return {
        "ocr_version": "PP-OCRv5",
        "text_detection_model_name": "PP-OCRv5_server_det",
        "text_recognition_model_name": "latin_PP-OCRv5_mobile_rec",
    }


def _resolve_paddle_device() -> str:
    # Map PADDLE_DEVICE env to PaddleOCR device string / Ánh xạ env sang chuỗi device của PaddleOCR
    requested = os.getenv("PADDLE_DEVICE", "auto").strip().lower()
    gpu_id = os.getenv("PADDLE_GPU_ID", "0").strip() or "0"
    if requested == "cpu":
        return "cpu"
    try:
        import paddle

        cuda_ready = paddle.device.is_compiled_with_cuda()
    except Exception:
        cuda_ready = False
    if requested == "gpu" and not cuda_ready:
        raise RuntimeError("PADDLE_DEVICE=gpu but paddlepaddle-gpu/CUDA is not available")
    if cuda_ready and requested in ("gpu", "auto"):
        return f"gpu:{gpu_id}"
    return "cpu"


def _build_ocr_engine(*, lang: str, profile: dict[str, str]) -> Any:
    # Construct PaddleOCR pipeline with shared defaults / Tạo pipeline PaddleOCR với default chung
    from paddleocr import PaddleOCR

    device = _resolve_paddle_device()
    use_mkldnn = device == "cpu"
    return PaddleOCR(
        lang=lang,
        device=device,
        use_textline_orientation=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        enable_mkldnn=use_mkldnn,
        **profile,
    )


class OcrRequest(BaseModel):
    image_base64: str


def _preprocess_image(image: np.ndarray) -> np.ndarray:
    # Optional CLAHE contrast boost for scanned pages / Tăng tương phản tùy chọn cho trang scan
    mode = os.getenv("PADDLE_PREPROCESS", "").strip().lower()
    if mode != "clahe":
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def _get_ocr_engine_ch() -> Any:
    # Lazy-load Chinese/Hán OCR engine / Lazy-load engine OCR tiếng Trung/Hán
    global _ocr_engine_ch, _resolved_device, _ocr_profile
    if _ocr_engine_ch is None:
        _resolved_device = _resolve_paddle_device()
        _ocr_profile = _resolve_ocr_models()
        _ocr_engine_ch = _build_ocr_engine(lang="ch", profile=_ocr_profile)
    return _ocr_engine_ch


def _get_ocr_engine_latin() -> Any:
    # Lazy-load Latin/Vietnamese OCR engine / Lazy-load engine OCR Latin/tiếng Việt
    global _ocr_engine_latin, _latin_profile
    if _ocr_engine_latin is None:
        _latin_profile = _resolve_latin_models()
        _ocr_engine_latin = _build_ocr_engine(lang=LATIN_LANG, profile=_latin_profile)
    return _ocr_engine_latin


def _downscale_image(image: np.ndarray, max_side: int = MAX_IMAGE_SIDE) -> np.ndarray:
    # Resize oversized scans before OCR / Thu nhỏ ảnh quét quá lớn trước khi OCR
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image
    scale = max_side / float(longest)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)


def _parse_ocr_results(raw_result: list[Any]) -> tuple[list[str], list[dict[str, Any]], list[float]]:
    # Normalize PaddleOCR 3.x and legacy 2.x outputs / Chuẩn hóa output PaddleOCR 3.x và 2.x
    lines: list[str] = []
    blocks: list[dict[str, Any]] = []
    confidences: list[float] = []

    for item in raw_result or []:
        payload: dict[str, Any] | None = None
        if hasattr(item, "json"):
            data = item.json
            payload = data.get("res", data) if isinstance(data, dict) else None
        elif isinstance(item, dict):
            payload = item.get("res", item)

        if payload:
            rec_texts = payload.get("rec_texts") or []
            rec_scores = payload.get("rec_scores") or []
            rec_polys = payload.get("rec_polys") or payload.get("dt_polys") or []

            if hasattr(rec_scores, "tolist"):
                rec_scores = rec_scores.tolist()
            if hasattr(rec_polys, "tolist"):
                rec_polys = rec_polys.tolist()

            for idx, text in enumerate(rec_texts):
                if not text:
                    continue
                confidence = float(rec_scores[idx]) if idx < len(rec_scores) else 0.0
                box = rec_polys[idx] if idx < len(rec_polys) else None
                if hasattr(box, "tolist"):
                    box = box.tolist()
                lines.append(str(text))
                confidences.append(confidence)
                blocks.append({"text": str(text), "confidence": confidence, "box": box})
            continue

        # Legacy PaddleOCR 2.x nested list format / Định dạng list lồng nhau của PaddleOCR 2.x
        if isinstance(item, (list, tuple)):
            for row in item:
                if not row or len(row) < 2:
                    continue
                box, text_conf = row[0], row[1]
                text, confidence = text_conf[0], float(text_conf[1])
                lines.append(str(text))
                confidences.append(confidence)
                blocks.append({"text": str(text), "confidence": confidence, "box": box})

    return lines, blocks, confidences


def _run_ocr(image: np.ndarray) -> tuple[list[dict[str, Any]], list[float]]:
    # Run Chinese pass and optional Latin pass, then merge / Chạy pass ch và latin (nếu bật) rồi merge
    ch_raw = _get_ocr_engine_ch().predict(image)
    _lines, ch_blocks, ch_confidences = _parse_ocr_results(ch_raw)

    if not _latin_enabled():
        return ch_blocks, ch_confidences

    latin_raw = _get_ocr_engine_latin().predict(image)
    _latin_lines, latin_blocks, _latin_confidences = _parse_ocr_results(latin_raw)
    merged_blocks = merge_dual_pass_blocks(ch_blocks, latin_blocks)
    confidences = [float(block.get("confidence", 0.0)) for block in merged_blocks]
    return merged_blocks, confidences


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Preload Chinese engine only; Latin is optional and VRAM-heavy on 6GB GPUs
    # Chỉ preload engine ch; Latin tốn VRAM trên GPU 6GB
    _get_ocr_engine_ch()
    yield


app = FastAPI(title="HVB PaddleOCR Service", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    cuda_compiled = False
    try:
        import paddle

        cuda_compiled = bool(paddle.device.is_compiled_with_cuda())
    except Exception:
        pass
    return {
        "status": "ok",
        "paddle_device": _resolved_device or "not_loaded",
        "cuda_compiled": cuda_compiled,
        "paddle_device_env": os.getenv("PADDLE_DEVICE", "auto"),
        "ocr_tier": OCR_TIER,
        "ocr_profile": _ocr_profile or _resolve_ocr_models(),
        "latin_enabled": _latin_enabled(),
        "latin_lang": LATIN_LANG,
        "latin_profile": _latin_profile or (_resolve_latin_models() if _latin_enabled() else {}),
    }


@app.post("/ocr")
def ocr_endpoint(request: OcrRequest) -> dict[str, Any]:
    try:
        # Decode PNG/JPEG and run PaddleOCR / Giải mã ảnh và chạy PaddleOCR
        image_bytes = base64.b64decode(request.image_base64)
        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Unable to decode image bytes")

        image = _downscale_image(image)
        image = _preprocess_image(image)
        blocks, confidences = _run_ocr(image)
        layout_text = reconstruct_text_from_blocks(blocks)
        lines = [str(block.get("text", "")) for block in blocks if block.get("text")]
        merged_text = layout_text if layout_text.strip() else "\n".join(lines)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return {
            "text": merged_text,
            "confidence": avg_confidence,
            "blocks": blocks,
        }
    except Exception as exc:
        # Log full traceback for 500 diagnostics / Ghi traceback đầy đủ khi lỗi 500
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
