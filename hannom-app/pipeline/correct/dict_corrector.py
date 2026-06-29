"""``dict`` corrector — shape-similarity repair using the project dictionaries.

Implements the S1∩S2 idea already used in the project (S1 = visual-similarity
set ``SinoNom_Similar.dic``; S2 = the valid SinoNom set = all characters that
appear as readings in ``QuocNgu_SinoNom.dic``) for the Han column only:

  For each OCR'd Han char, if the char is NOT in the valid SinoNom set (S2) but a
  shape-similar char (S1) IS, replace it with the most-similar valid candidate.

This is conservative — characters already in the valid set are never touched, and
a char with no valid similar candidate is left as-is. The dicts live in
``DICTS_DIR`` (mounted volume); if absent, this corrector is a no-op.
"""

from __future__ import annotations

import logging
import os

from pipeline.correct import register

logger = logging.getLogger("hannom.correct.dict")


def _load_dic(path: str) -> dict[str, list[str]]:
    """Parse a ``key:val1 val2 …`` dictionary file (skips # comments)."""
    out: dict[str, list[str]] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or ":" not in line:
                continue
            key, _, rest = line.partition(":")
            out[key.strip()] = rest.split()
    return out


class DictCorrector:
    name = "dict"

    def __init__(self, config=None) -> None:
        dicts_dir = getattr(config, "dicts_dir", "./dicts") if config else "./dicts"
        similar = _load_dic(os.path.join(dicts_dir, "SinoNom_Similar.dic"))
        quocngu = _load_dic(os.path.join(dicts_dir, "QuocNgu_SinoNom.dic"))
        self._similar = similar  # char -> [shape-similar chars]
        # Valid SinoNom set = every char that has a Quốc Ngữ reading (S2).
        self._valid: set[str] = {c for chars in quocngu.values() for c in chars}
        # Also treat the keys of the similarity dict as known characters.
        self._valid.update(similar.keys())
        if not self._valid:
            logger.warning(
                "DictCorrector: no dictionaries found under %r; correction is a "
                "no-op. (CORRECT_BACKEND=dict needs SinoNom_Similar.dic + "
                "QuocNgu_SinoNom.dic in DICTS_DIR.)",
                dicts_dir,
            )

    def correct(self, han: str, context: str = "") -> str:  # noqa: ARG002
        if not self._valid:
            return han
        out: list[str] = []
        for ch in han:
            if ch in self._valid or not ("一" <= ch <= "鿿"):
                out.append(ch)  # already valid, or punctuation/non-CJK
                continue
            # OCR char not in the valid set: try a shape-similar valid candidate.
            candidate = next(
                (s for s in self._similar.get(ch, []) if s in self._valid), None
            )
            out.append(candidate if candidate else ch)
        return "".join(out)


register("dict", DictCorrector)
