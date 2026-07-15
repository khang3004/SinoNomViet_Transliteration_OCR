from __future__ import annotations

import re
from typing import Any

CONFIDENCE_METHOD = "heuristic_entry_v1"

# Literal garbage often returned by vision models / Chuỗi rác model vision hay trả
_GARBAGE_LITERALS = {"none", "null", "n/a", "na", "-", "—"}

# TOC section headers mistaken as trich_yeu body / Nhãn mục lục bị nhầm là nội dung TY
_TRICH_YEU_LABEL_ONLY = re.compile(
    r"^\s*(trích\s*yếu|trich\s*yeu|trích\s*yêú)\s*\.?\s*$",
    flags=re.IGNORECASE,
)

# Likely mid-phrase endings (page cut) / Kết thúc giữa cụm (nghi cắt trang)
_TRUNC_TAIL = re.compile(
    r"(及|以|之|與|而|於|各項材|諸|等|và|của|để|cho|với|theo|thì|,|、$)$"
)


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _GARBAGE_LITERALS:
        return ""
    # Strip label-only "TRÍCH YẾU" → treat as empty body /
    # Chỉ còn nhãn TRÍCH YẾU → coi như chưa có nội dung
    if _TRICH_YEU_LABEL_ONLY.match(text):
        return ""
    return text


def clean_trich_yeu(trich: Any) -> dict[str, str]:
    # Normalize trich_yeu and strip garbage literals / Chuẩn hóa trích yếu, bỏ literal rác
    if not isinstance(trich, dict):
        return {"han_nom": "", "quoc_ngu": ""}
    return {
        "han_nom": _norm_text(trich.get("han_nom")),
        "quoc_ngu": _norm_text(trich.get("quoc_ngu")),
    }


def is_cut_at_trich_yeu_label(
    *,
    trich_yeu: dict[str, str] | None = None,
    is_last_on_page: bool = False,
    flags: list[str] | None = None,
) -> bool:
    """True when page ends at TRÍCH YẾU header with no body yet.

    True khi hết trang đúng nhãn TRÍCH YẾU, chưa có thân trích yếu.
    """
    flag_set = set(flags or [])
    if "cut_at_trich_yeu_label" in flag_set:
        return True
    trich = clean_trich_yeu(trich_yeu)
    empty = not trich["han_nom"] and not trich["quoc_ngu"]
    return bool(is_last_on_page and empty)


def detect_entry_flags(
    *,
    chi_muc: dict[str, Any] | None,
    trich_yeu: dict[str, str],
    is_last_on_page: bool = False,
) -> list[str]:
    # Collect quality flags for one STT entry / Thu thập cờ chất lượng cho một entry STT
    flags: list[str] = []
    chi = chi_muc or {}
    # Re-clean so label-only strings become empty / Làm sạch lại để nhãn-only thành rỗng
    trich = clean_trich_yeu(trich_yeu)
    hn = trich.get("han_nom", "")
    qn = trich.get("quoc_ngu", "")
    de_tai = _norm_text(chi.get("de_tai"))

    if not hn and not qn:
        flags.append("empty_trich_yeu")
    elif not hn:
        flags.append("missing_han_nom")
    elif not qn:
        flags.append("missing_quoc_ngu")

    if not de_tai:
        flags.append("missing_de_tai")

    for label, text in (("han_nom", hn), ("quoc_ngu", qn)):
        if not text:
            continue
        # Only hard mid-phrase endings count as truncated / Chỉ cụm giữa câu mới coi là cắt
        if _TRUNC_TAIL.search(text.rstrip()):
            flags.append(f"truncated_{label}")

    # Last entry ending at TRÍCH YẾU label OR mid-metadata cut → must stitch /
    # Entry cuối cắt ở nhãn TY hoặc giữa metadata → bắt buộc stitch
    if is_last_on_page and (
        "empty_trich_yeu" in flags
        or "missing_de_tai" in flags
        or any(f.startswith("truncated_") for f in flags)
    ):
        flags.append("needs_stitch")
        if "empty_trich_yeu" in flags and "missing_de_tai" not in flags:
            flags.append("cut_at_trich_yeu_label")
        if "missing_de_tai" in flags and "empty_trich_yeu" in flags:
            flags.append("cut_mid_entry")

    if "empty_trich_yeu" in flags or "missing_de_tai" in flags:
        flags.append("needs_review")
    elif any(f.startswith("truncated_") for f in flags):
        flags.append("needs_review")

    return sorted(set(flags))


def estimate_entry_ocr_confidence(
    *,
    chi_muc: dict[str, Any] | None,
    trich_yeu: dict[str, str],
    flags: list[str] | None = None,
    parse_ok: bool = True,
) -> float:
    """Heuristic entry confidence for TOC OCR (0–1).

    Điểm tin cậy heuristic cho entry mục lục OCR (0–1).
    """
    flags = flags or detect_entry_flags(chi_muc=chi_muc, trich_yeu=trich_yeu)
    chi = chi_muc or {}
    hn = trich_yeu.get("han_nom", "")
    qn = trich_yeu.get("quoc_ngu", "")

    score = 0.0
    if parse_ok:
        score += 0.25
    if _norm_text(chi.get("de_tai")):
        score += 0.15
    if _norm_text(chi.get("ngay_thang")):
        score += 0.05
    if _norm_text(chi.get("the_loai")):
        score += 0.05
    if hn and qn:
        score += 0.30
    elif hn or qn:
        score += 0.12
    # Reward preserved line breaks / Thưởng khi giữ xuống dòng
    line_breaks = hn.count("\n") + qn.count("\n")
    if line_breaks > 0:
        score += min(0.10, 0.03 * line_breaks)
    if "empty_trich_yeu" in flags:
        score = min(score, 0.20)
    if "needs_stitch" in flags:
        score = min(score, 0.55)
    if any(f.startswith("truncated_") for f in flags):
        score -= 0.15
    if "missing_de_tai" in flags:
        score -= 0.20
    return round(min(0.99, max(0.0, score)), 4)


def derive_entry_status(flags: list[str], *, refined: bool = False, stitched: bool = False) -> str:
    # Map flags to lifecycle status / Map cờ sang trạng thái vòng đời entry
    if "empty_trich_yeu" in flags or "missing_de_tai" in flags:
        return "incomplete"
    if "needs_stitch" in flags or any(f.startswith("truncated_") for f in flags):
        return "needs_stitch" if not stitched else "stitched"
    if "needs_review" in flags:
        return "needs_review"
    if stitched:
        return "stitched"
    if refined:
        return "refined"
    return "catalog_ok"


def should_refine_entry(ocr_confidence: float, flags: list[str], threshold: float = 0.7) -> bool:
    # Decide whether DeepSeek refine should run / Quyết định có chạy refine DeepSeek không
    if ocr_confidence < threshold:
        return True
    heavy = {"empty_trich_yeu", "missing_de_tai", "needs_review", "needs_stitch"}
    return bool(heavy.intersection(flags)) or any(f.startswith("truncated_") for f in flags)


def effective_confidence(entry: dict[str, Any]) -> float:
    # Best available confidence across OCR/refine/stitch / Lấy điểm tin cậy tốt nhất trong các lớp
    values: list[float] = []
    for key in ("refine_confidence", "stitch_confidence", "ocr_confidence"):
        raw = entry.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            values.append(val)
    if values:
        return max(values)
    trich = clean_trich_yeu(entry.get("trich_yeu"))
    if trich["han_nom"] or trich["quoc_ngu"]:
        # Partial text still searchable / Text một phần vẫn search được
        return 0.5
    return 0.0


def should_index_entry(entry: dict[str, Any], min_confidence: float = 0.45) -> bool:
    """Index when entry has real text or at least đề tài metadata.

    Index khi có chữ thật hoặc còn đề tài (fallback catalog-only).
    """
    trich = clean_trich_yeu(entry.get("trich_yeu"))
    chi = entry.get("chi_muc") if isinstance(entry.get("chi_muc"), dict) else {}
    de_tai = str((chi or {}).get("de_tai") or "").strip()
    if de_tai.lower() in {"none", "null"}:
        de_tai = ""
    has_body = bool(trich["han_nom"] or trich["quoc_ngu"])
    if not has_body and not de_tai:
        return False
    conf_f = effective_confidence(entry)
    flags = entry.get("flags") or []
    # Page-break / review / catalog-only đề tài: still index /
    # Cắt trang / review / chỉ có đề tài: vẫn index
    if (
        has_body
        and (
            any(f.startswith("truncated_") for f in flags)
            or entry.get("status") in {"needs_stitch", "stitched", "needs_review", "partial", "incomplete"}
            or len(entry.get("source_pages") or []) > 1
        )
    ):
        return True
    if not has_body and de_tai:
        # Allow searchable stub from catalog metadata / Cho phép stub tìm được từ metadata chỉ mục
        return True
    return conf_f >= min_confidence


def source_page_nos(entry: dict[str, Any]) -> list[int]:
    # Collect all page_no values for Qdrant payload / Gom mọi page_no đưa vào payload Qdrant
    pages: list[int] = []
    for row in entry.get("source_pages") or []:
        if not isinstance(row, dict) or row.get("page_no") is None:
            continue
        try:
            page = int(row["page_no"])
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    return pages
