from __future__ import annotations

from io import BytesIO

from common.config import get_value, load_config


def _as_bool(raw: str, *, default: bool) -> bool:
    # Parse config boolean / Parse boolean từ config
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _text_protect_mask(gray, cv2) -> object:
    # Mask ink strokes so inpaint skips real text / Mask nét chữ để không xóa nhầm
    _, text = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    protect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cv2.dilate(text, protect_kernel, iterations=1)


def _mask_coverage_ratio(mask, cv2, np) -> float:
    # Fraction of pixels marked for inpaint / Tỷ lệ pixel trong mask inpaint
    total = int(mask.shape[0] * mask.shape[1])
    if total <= 0:
        return 0.0
    return float(cv2.countNonZero(mask)) / float(total)


def _inpaint_mask(gray, watermark_mask, cv2, np) -> object:
    # Inpaint only small masks; large masks smear the page / Chỉ inpaint mask nhỏ
    cfg = load_config()
    inpaint_radius = int(
        get_value(cfg, "opencv_preprocess", "watermark_inpaint_radius", fallback="3")
    )
    inpaint_passes = int(
        get_value(cfg, "opencv_preprocess", "watermark_inpaint_passes", fallback="1")
    )
    dilate_px = int(get_value(cfg, "opencv_preprocess", "watermark_dilate_px", fallback="2"))
    max_ratio = float(
        get_value(cfg, "opencv_preprocess", "watermark_max_inpaint_ratio", fallback="0.08")
    )

    if cv2.countNonZero(watermark_mask) == 0:
        return gray

    dilate_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (max(dilate_px, 1), max(dilate_px, 1)),
    )
    watermark_mask = cv2.dilate(watermark_mask, dilate_kernel, iterations=1)

    if _mask_coverage_ratio(watermark_mask, cv2, np) > max_ratio:
        # Skip inpaint on huge masks to avoid vertical smearing / Bỏ inpaint mask quá lớn
        print(
            "[opencv_preprocess] skip inpaint: mask covers "
            f"{_mask_coverage_ratio(watermark_mask, cv2, np) * 100:.1f}% (> {max_ratio * 100:.1f}%)"
        )
        return gray

    result = gray
    for _ in range(max(inpaint_passes, 1)):
        result = cv2.inpaint(
            result,
            watermark_mask,
            inpaintRadius=max(inpaint_radius, 1),
            flags=cv2.INPAINT_TELEA,
        )
    return result


def _large_blob_mask(binary_mask, cv2, np, *, min_area: int, max_area: int | None = None) -> object:
    # Keep blob sizes in range / Giữ cụm vết trong khoảng diện tích
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary_mask, connectivity=8)
    output = np.zeros_like(binary_mask)
    for label_idx in range(1, num_labels):
        area = int(stats[label_idx, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        if max_area is not None and area > max_area:
            continue
        output[labels == label_idx] = 255
    return output


def _gentle_illumination_correct(gray, cv2, np) -> object:
    # Mild background flatten for gray stamps (no inpaint) / Chuẩn hóa nền nhẹ cho watermark xám
    cfg = load_config()
    sigma = float(get_value(cfg, "opencv_preprocess", "watermark_flatten_sigma", fallback="22"))
    blend = float(get_value(cfg, "opencv_preprocess", "watermark_flatten_blend", fallback="0.45"))
    if sigma <= 0:
        return gray

    blend = min(max(blend, 0.0), 1.0)
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    blur = np.maximum(blur.astype(np.float32), 1.0)
    corrected = (gray.astype(np.float32) / blur) * 185.0
    corrected = np.clip(corrected, 0, 255)
    mixed = (blend * corrected) + ((1.0 - blend) * gray.astype(np.float32))
    return np.clip(mixed, 0, 255).astype(np.uint8)


def _dark_watermark_mask(gray, cv2, np) -> object:
    # Detect dark fan/smudges via black-hat / Phát hiện vết tối fan bằng black-hat
    cfg = load_config()
    kernel_size = int(
        get_value(cfg, "opencv_preprocess", "watermark_blackhat_kernel", fallback="35")
    )
    blackhat_threshold = int(
        get_value(cfg, "opencv_preprocess", "watermark_blackhat_threshold", fallback="12")
    )
    min_blob_area = int(
        get_value(cfg, "opencv_preprocess", "watermark_min_blob_area", fallback="350")
    )
    max_blob_area = int(
        get_value(cfg, "opencv_preprocess", "watermark_max_blob_area", fallback="25000")
    )

    kernel_size = kernel_size if kernel_size % 2 == 1 else kernel_size + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    _, stain_mask = cv2.threshold(
        blackhat,
        blackhat_threshold,
        255,
        cv2.THRESH_BINARY,
    )

    open_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    stain_mask = cv2.morphologyEx(stain_mask, cv2.MORPH_OPEN, open_kernel, iterations=2)
    stain_mask = _large_blob_mask(
        stain_mask,
        cv2,
        np,
        min_area=min_blob_area,
        max_area=max_blob_area,
    )

    text_mask = _text_protect_mask(gray, cv2)
    return cv2.bitwise_and(stain_mask, cv2.bitwise_not(text_mask))


def _remove_watermark_gray(gray, cv2, np) -> object:
    # Light stamp: gentle flatten only; dark fan: small-area inpaint / Xám: flatten nhẹ; fan: inpaint nhỏ
    cfg = load_config()
    mode = get_value(cfg, "opencv_preprocess", "watermark_mode", fallback="light").strip().lower()

    working = gray
    if mode in {"light", "both"}:
        working = _gentle_illumination_correct(working, cv2, np)

    if mode in {"dark", "both"}:
        dark_mask = _dark_watermark_mask(working, cv2, np)
        working = _inpaint_mask(working, dark_mask, cv2, np)

    return working


def denoise_png_bytes(image_bytes: bytes) -> bytes:
    """Denoise scanned page PNG with OpenCV for cleaner OCR input.

    Lọc nhiễu ảnh scan bằng OpenCV trước khi OCR.
    """
    try:
        import cv2
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'opencv-python-headless'. Install dags/requirements.txt."
        ) from exc

    cfg = load_config()
    remove_watermark = _as_bool(
        get_value(cfg, "opencv_preprocess", "remove_watermark", fallback="true"),
        default=True,
    )
    denoise_strength = int(get_value(cfg, "opencv_preprocess", "denoise_strength", fallback="7"))
    use_clahe = _as_bool(get_value(cfg, "opencv_preprocess", "use_clahe", fallback="true"), default=True)
    clahe_clip = float(get_value(cfg, "opencv_preprocess", "clahe_clip_limit", fallback="1.5"))
    bilateral_d = int(get_value(cfg, "opencv_preprocess", "bilateral_d", fallback="3"))

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Unable to decode PNG bytes for OpenCV denoise")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if remove_watermark:
        gray = _remove_watermark_gray(gray, cv2, np)

    denoised = cv2.fastNlMeansDenoising(
        gray,
        None,
        h=denoise_strength,
        templateWindowSize=7,
        searchWindowSize=21,
    )

    if use_clahe:
        clahe = cv2.createCLAHE(clipLimit=max(clahe_clip, 1.0), tileGridSize=(8, 8))
        denoised = clahe.apply(denoised)

    if bilateral_d > 0:
        denoised = cv2.bilateralFilter(denoised, bilateral_d, 40, 40)

    output_bgr = cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR)
    ok, encoded = cv2.imencode(".png", output_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RuntimeError("OpenCV failed to encode denoised PNG")

    try:
        from PIL import Image

        Image.open(BytesIO(encoded.tobytes())).verify()
    except Exception as exc:
        raise RuntimeError("Denoised PNG is not valid") from exc

    return encoded.tobytes()
