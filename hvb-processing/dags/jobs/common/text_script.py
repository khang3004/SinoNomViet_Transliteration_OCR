from __future__ import annotations

import re

# CJK unified + compatibility ranges / Dải ký tự CJK thống nhất và tương thích
_CJK_CHAR = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
# Latin letters with Vietnamese diacritics / Chữ Latin kèm dấu tiếng Việt
_LATIN_CHAR = re.compile(r"[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]")


def script_ratios(text: str) -> tuple[float, float, int]:
    # Return (cjk_ratio, latin_ratio, char_count) / Trả về tỷ lệ CJK, Latin và số ký tự
    compact = "".join(character for character in text if not character.isspace())
    if not compact:
        return 0.0, 0.0, 0
    cjk_count = len(_CJK_CHAR.findall(compact))
    latin_count = len(_LATIN_CHAR.findall(compact))
    total = len(compact)
    return cjk_count / total, latin_count / total, total


def latin_ratio(text: str) -> float:
    # Latin character ratio over non-space chars / Tỷ lệ ký tự Latin trên tổng ký tự
    _cjk_ratio, latin, _total = script_ratios(text)
    return latin


def cjk_ratio(text: str) -> float:
    # CJK character ratio over non-space chars / Tỷ lệ ký tự CJK trên tổng ký tự
    cjk, _latin, _total = script_ratios(text)
    return cjk
