from __future__ import annotations

import re
from typing import Any

# Regex fixes for recurring HVB metadata phrases / Sửa cụm metadata HVB lặp lại sau OCR Latin
_METADATA_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\bCUC\s*VAN\s*THU\s*VA\s*LUU\s*TRU\s*NHA\s*NUOC\b",
            re.IGNORECASE,
        ),
        "CỤC VĂN THƯ VÀ LƯU TRỮ NHÀ NƯỚC",
    ),
    (re.compile(r"\bCUC\s*V[ÊE]?N?\s*THU\b", re.IGNORECASE), "CỤC VĂN THƯ"),
    (re.compile(r"\bLUU\s*TR[ƯU]?\s*NH[AÀ]\s*NUC\b", re.IGNORECASE), "LƯU TRỮ NHÀ NƯỚC"),
    (re.compile(r"\bLUUTRU[VWＮ]?\s*Ｎ?\b", re.IGNORECASE), "LƯU TRỮ NHÀ NƯỚC"),
    (re.compile(r"\bHUCLUHC\b|\bHUC\s*LUHC\b|\bMUCLUC\b", re.IGNORECASE), "MỤC LỤC"),
    (
        re.compile(r"\bCHAUBAN\s*TRIEU\s*NGUYEN\b|\bCHAUBANTRIEUNGUYEN\b", re.IGNORECASE),
        "CHÂU BẢN TRIỀU NGUYỄN",
    ),
    (re.compile(r"\bCH[AÂ]U\s*B[AẢ]?N?\b", re.IGNORECASE), "CHÂU BẢN"),
    (re.compile(r"\bTRIU\s*NGUY[EÊỄ]N\b|\bTRI[EÊ]U\s*NGUY[EÊỄ]N\b", re.IGNORECASE), "TRIỀU NGUYỄN"),
    (re.compile(r"\bTAPI\b|\bTAP\s*I\b", re.IGNORECASE), "TẬP I"),
    (re.compile(r"\bT[AÂ]P\s*1\b", re.IGNORECASE), "TẬP 1"),
    (re.compile(r"\bGIALONG\b", re.IGNORECASE), "GIA LONG"),
    (re.compile(r"\bMINH\s*MENHI\b", re.IGNORECASE), "MINH MỆNH I"),
    (re.compile(r"\bMINH\s*MENHV\b", re.IGNORECASE), "MINH MỆNH V"),
    (re.compile(r"MINH1MNH", re.IGNORECASE), "MINH MỆNH"),
    (re.compile(r"\bMINH\s*M[EÊ]NH\b", re.IGNORECASE), "MINH MỆNH"),
    (re.compile(r"\bNH[AÀ]\s*XU[AẤ]T\s*B[AẢ]N\b", re.IGNORECASE), "NHÀ XUẤT BẢN"),
    (re.compile(r"\bVN\s*HO\b", re.IGNORECASE), "VĂN HOÁ"),
    (re.compile(r"\bV[AĂ]N\s*HO[AÁ]\b", re.IGNORECASE), "VĂN HOÁ"),
    (re.compile(r"\bTH[OÔ]NG\s*TIN\b", re.IGNORECASE), "THÔNG TIN"),
    (re.compile(r"\bXU[AẤ]T\s*B[AẢ]N\b", re.IGNORECASE), "XUẤT BẢN"),
]


def fix_metadata_line(text: str) -> str:
    # Apply phrase corrections on one OCR line / Sửa một dòng OCR metadata
    fixed = text
    for pattern, replacement in _METADATA_RULES:
        fixed = pattern.sub(replacement, fixed)
    # Collapse duplicate spaces after replacements / Gom khoảng trắng thừa
    return re.sub(r" {2,}", " ", fixed).strip()


def apply_vi_metadata_fix(text: str, blocks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    # Fix page text and per-block Latin metadata / Sửa text trang và từng block Latin metadata
    fixed_blocks: list[dict[str, Any]] = []
    for block in blocks:
        block_text = str(block.get("text", ""))
        source = str(block.get("source", "")).lower()
        if source == "latin" or _looks_latin(block_text):
            block_text = fix_metadata_line(block_text)
        fixed_blocks.append({**block, "text": block_text})

    fixed_lines = [fix_metadata_line(line) for line in text.splitlines()]
    fixed_text = "\n".join(line for line in fixed_lines if line)
    if not fixed_text.strip() and fixed_blocks:
        fixed_text = "\n".join(
            str(block.get("text", "")) for block in fixed_blocks if str(block.get("text", "")).strip()
        )
    return fixed_text, fixed_blocks


def _looks_latin(text: str) -> bool:
    # Heuristic: metadata lines are mostly Latin digits / Heuristic: dòng metadata chủ yếu Latin/số
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        return False
    latinish = sum(ch.isascii() and ch.isalnum() for ch in compact)
    return latinish / len(compact) >= 0.6
