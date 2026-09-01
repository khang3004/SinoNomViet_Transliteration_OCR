"""The study sample: the fixed set of images this audit is actually about.

The corpus is roughly nine thousand images. Reviewing all of them is not the
plan — the plan is to review **500**, drawn once, and report what that sample
says about the whole. So the sample is a first-class object rather than a
side effect of who clicked "get images" first:

* it is drawn **once**, stratified across the disagreement bands and capped per
  post, and then it is the denominator of every progress bar on the page;
* reviewers are assigned from **inside** it, never from the corpus at large;
* an image that turns out to be unusable is **dropped and replaced**, so the
  study still ends with 500 judged images rather than 493.

That last point is why membership is an append-only log rather than a list: a
dropped record must stay dropped (re-drawing it would hand the same broken image
to the next reviewer) while its replacement is recorded alongside.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.core import sampling
from app.core.jsonlog import JsonlLog, read_json, write_json
from app.core.models import Band, CorpusRecord

log = logging.getLogger(__name__)

DEFAULT_SIZE = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SampleDraw:
    """What a draw or a top-up actually managed to do."""

    added: list[CorpusRecord]
    requested: int
    per_band: Counter
    shortfall: Counter
    blocked_by_post_cap: int = 0

    @property
    def short(self) -> int:
        return self.requested - len(self.added)

    def to_json(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "added": len(self.added),
            "short": self.short,
            "per_band": {str(k): v for k, v in self.per_band.items()},
            "shortfall": {str(k): v for k, v in self.shortfall.items()},
            "blocked_by_post_cap": self.blocked_by_post_cap,
        }


class SampleStore:
    """Membership of the study sample, as an append-only log."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.log = JsonlLog(self.data_dir / "sample.jsonl")
        self.meta_path = self.data_dir / "sample.json"

    # --- metadata --------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        return read_json(self.meta_path, default={}) or {}

    @property
    def exists(self) -> bool:
        return bool(self.meta())

    @property
    def size(self) -> int:
        """The target: how many judged images this study is meant to produce."""
        return int(self.meta().get("size", 0))

    def targets(self) -> dict[Band, float]:
        raw = self.meta().get("targets") or {}
        parsed: dict[Band, float] = {}
        for name, weight in raw.items():
            try:
                parsed[Band(name)] = float(weight)
            except (ValueError, TypeError):
                continue
        return parsed or dict(sampling.DEFAULT_TARGETS)

    @property
    def per_post_cap(self) -> int:
        return int(self.meta().get("per_post_cap", sampling.DEFAULT_PER_POST_CAP))

    # --- membership ------------------------------------------------------

    def _state(self) -> dict[str, dict[str, Any]]:
        return self.log.fold(lambda row: row["record_id"])

    def members(self) -> set[str]:
        """Record ids currently in the study."""
        return {
            rid for rid, row in self._state().items() if not row.get("dropped")
        }

    def dropped(self) -> dict[str, str]:
        """Record ids removed from the study, and why."""
        return {
            rid: row.get("reason", "")
            for rid, row in self._state().items()
            if row.get("dropped")
        }

    def touched(self) -> set[str]:
        """Everything ever drawn — members plus drops.

        A top-up must not re-draw a record that was dropped as unusable, so
        eligibility is checked against this rather than against members alone.
        """
        return set(self._state())

    def band_counts(self, records_by_id: dict[str, CorpusRecord]) -> Counter:
        counts: Counter = Counter()
        for record_id in self.members():
            record = records_by_id.get(record_id)
            if record is not None:
                counts[record.band] += 1
        return counts

    def post_counts(self, records_by_id: dict[str, CorpusRecord]) -> dict[str, int]:
        """Images per post already in the study — what the cap is counted against."""
        counts: Counter = Counter()
        for record_id in self.members():
            record = records_by_id.get(record_id)
            if record is not None:
                counts[record.post_id] += 1
        return dict(counts)

    # --- mutation --------------------------------------------------------

    def reset(
        self,
        size: int,
        targets: dict[Band, float],
        per_post_cap: int,
        created_by: str = "",
    ) -> None:
        """Start a new study. Clears membership; reviews are untouched."""
        self.log.clear()
        write_json(
            self.meta_path,
            {
                "size": size,
                "targets": {b.value: round(w, 6) for b, w in targets.items()},
                "per_post_cap": per_post_cap,
                "created_at": _now(),
                "created_by": created_by,
            },
        )

    def add(self, records: Sequence[CorpusRecord], reason: str = "draw") -> None:
        if not records:
            return
        self.log.extend(
            [
                {
                    "record_id": r.record_id,
                    "band": r.band.value,
                    "post_id": r.post_id,
                    "added_at": _now(),
                    "reason": reason,
                    "dropped": False,
                }
                for r in records
            ]
        )

    def drop(self, record_id: str, band: Band, reason: str) -> None:
        self.log.append(
            {
                "record_id": record_id,
                "band": band.value,
                "added_at": _now(),
                "reason": reason,
                "dropped": True,
            }
        )

    # --- drawing ---------------------------------------------------------

    def draw_into(
        self,
        corpus: Sequence[CorpusRecord],
        *,
        count: int,
        rng: random.Random | None = None,
        reason: str = "draw",
    ) -> SampleDraw:
        """Add ``count`` records to the study, keeping its shape.

        Bands are filled by deficit against the study's plan, so a top-up after
        three ``poor`` images were dropped pulls three more ``poor`` images —
        not three of whatever happens to be plentiful.
        """
        if count <= 0:
            return SampleDraw([], 0, Counter(), Counter())

        by_id = {r.record_id: r for r in corpus}
        seen = self.touched()
        eligible = [r for r in corpus if r.record_id not in seen]

        result = sampling.draw(
            eligible,
            n=count,
            targets=self._deficit_weights(by_id, count),
            per_post_cap=self.per_post_cap,
            post_counts=self.post_counts(by_id),
            rng=rng,
        )
        self.add(result.records, reason=reason)
        return SampleDraw(
            added=result.records,
            requested=count,
            per_band=result.per_band,
            shortfall=result.shortfall,
            blocked_by_post_cap=result.blocked_by_post_cap,
        )

    def _deficit_weights(
        self, records_by_id: dict[str, CorpusRecord], count: int
    ) -> dict[Band, float]:
        """How far each band is below its share of the study, as draw weights."""
        plan = sampling.quotas(self.size, self.targets())
        held = self.band_counts(records_by_id)
        deficits = {
            band: max(0, want - held.get(band, 0)) for band, want in plan.items()
        }
        if sum(deficits.values()) <= 0:
            # The study is already at plan — an overfill (admin raised the size
            # mid-study) just follows the original shares.
            return self.targets()
        return {band: float(n) for band, n in deficits.items()}

    # --- reporting -------------------------------------------------------

    def status(self, records_by_id: dict[str, CorpusRecord]) -> dict[str, Any]:
        meta = self.meta()
        members = self.members()
        return {
            "exists": bool(meta),
            "size": self.size,
            "active": len(members),
            "dropped": len(self.dropped()),
            "complete": len(members) >= self.size,
            "created_at": meta.get("created_at", ""),
            "created_by": meta.get("created_by", ""),
            "per_post_cap": self.per_post_cap,
            "posts": len(self.post_counts(records_by_id)),
        }
