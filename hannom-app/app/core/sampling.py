"""Drawing a review sample that is random but not uniform.

A uniform draw over nine thousand images spends its effort where there is least
to learn. Most rows are ones where the upstream Gemini output and the upstream
ground truth already agree; confirming those consumes reviewer hours and moves
no number. So the draw is **stratified on disagreement**: every sample gets a
fixed share of identical, near, diverging, far-apart, and one-side-empty rows.

Two constraints ride along:

* **Per-post cap.** A single prolific page can contribute dozens of images. Left
  alone the sample would describe that page rather than the corpus, so no post
  contributes more than ``per_post_cap`` images across the *whole* audit.
* **No overlap.** Reviewers never share a record, so the cap is counted globally
  rather than per reviewer, and a record drawn for one reviewer is gone.

Shortfalls redistribute. If only forty ``exact`` rows remain but the quota asks
for sixty, the missing twenty are handed to the bands that still have depth,
rather than silently returning a short sample.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.core.models import Band, CorpusRecord

log = logging.getLogger(__name__)

# Oversample disagreement. These are the shares of any sample, not of the corpus.
DEFAULT_TARGETS: dict[Band, float] = {
    Band.EXACT: 0.15,
    Band.NEAR: 0.20,
    Band.FAR: 0.25,
    Band.POOR: 0.25,
    Band.EMPTY: 0.15,
}

DEFAULT_PER_POST_CAP = 2


def normalize_targets(targets: dict[Band, float] | None) -> dict[Band, float]:
    """Coerce to shares summing to 1. Falls back to the default if unusable."""
    if not targets:
        return dict(DEFAULT_TARGETS)
    clean = {band: max(0.0, float(w)) for band, w in targets.items()}
    total = sum(clean.values())
    if total <= 0:
        return dict(DEFAULT_TARGETS)
    return {band: weight / total for band, weight in clean.items()}


def quotas(n: int, targets: dict[Band, float]) -> dict[Band, int]:
    """Split ``n`` across bands by largest remainder.

    Plain rounding of five shares routinely lands on n-1 or n+2. Largest
    remainder always sums to exactly n, which matters because the caller
    promises the reviewer a specific number of images.
    """
    if n <= 0:
        return {band: 0 for band in targets}
    shares = normalize_targets(targets)
    exact = {band: n * weight for band, weight in shares.items()}
    floors = {band: int(value) for band, value in exact.items()}

    remaining = n - sum(floors.values())
    if remaining > 0:
        order = sorted(shares, key=lambda b: (-(exact[b] - floors[b]), b.value))
        for band in order[:remaining]:
            floors[band] += 1
    return floors


@dataclass
class SampleResult:
    records: list[CorpusRecord] = field(default_factory=list)
    requested: int = 0
    per_band: Counter = field(default_factory=Counter)
    shortfall: Counter = field(default_factory=Counter)
    blocked_by_post_cap: int = 0

    @property
    def short(self) -> int:
        return self.requested - len(self.records)

    def to_json(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "drawn": len(self.records),
            "short": self.short,
            "per_band": {b.value if isinstance(b, Band) else b: c
                         for b, c in self.per_band.items()},
            "shortfall": {b.value if isinstance(b, Band) else b: c
                          for b, c in self.shortfall.items()},
            "blocked_by_post_cap": self.blocked_by_post_cap,
        }


def draw(
    available: Iterable[CorpusRecord],
    *,
    n: int,
    targets: dict[Band, float] | None = None,
    per_post_cap: int = DEFAULT_PER_POST_CAP,
    post_counts: dict[str, int] | None = None,
    rng: random.Random | None = None,
) -> SampleResult:
    """Draw ``n`` records, stratified by band and capped per post.

    ``post_counts`` is how many images each post has *already* contributed to
    the audit; the cap is enforced against that running total, not against this
    draw alone.
    """
    shares = normalize_targets(targets)
    result = SampleResult(requested=max(0, n))
    if n <= 0:
        return result

    chooser = rng or random.Random()
    taken = Counter(post_counts or {})

    pools: dict[Band, list[CorpusRecord]] = {band: [] for band in shares}
    for record in available:
        pools.setdefault(record.band, []).append(record)
    for pool in pools.values():
        chooser.shuffle(pool)

    def take(band: Band, want: int) -> list[CorpusRecord]:
        """Pull up to ``want`` records from one band, honouring the post cap."""
        picked: list[CorpusRecord] = []
        pool = pools.get(band) or []
        keep: list[CorpusRecord] = []
        for record in pool:
            if len(picked) >= want:
                keep.append(record)
                continue
            if per_post_cap > 0 and taken[record.post_id] >= per_post_cap:
                result.blocked_by_post_cap += 1
                # Dropped, not kept: the cap will still be hit on the next pass,
                # so re-testing it would just re-count the same block.
                continue
            taken[record.post_id] += 1
            picked.append(record)
        pools[band] = keep
        return picked

    plan = quotas(n, shares)
    drawn: list[CorpusRecord] = []
    for band, want in plan.items():
        got = take(band, want)
        drawn.extend(got)
        result.per_band[band] += len(got)
        if len(got) < want:
            result.shortfall[band] += want - len(got)

    # Redistribute what the thin bands could not supply. Bands are retried in
    # descending depth so one pass usually closes the gap.
    missing = n - len(drawn)
    while missing > 0:
        deepest = sorted(pools, key=lambda b: -len(pools[b]))
        progressed = False
        for band in deepest:
            if missing <= 0:
                break
            got = take(band, missing)
            if got:
                drawn.extend(got)
                result.per_band[band] += len(got)
                missing -= len(got)
                progressed = True
        if not progressed:
            break

    # Interleave the bands so a reviewer does not face sixty identical-match
    # rows in a row and then sixty hard ones — that skews attention, and the
    # tail of a session is where errors cluster.
    chooser.shuffle(drawn)
    result.records = drawn
    if result.short:
        log.info("sample short by %d of %d requested", result.short, n)
    return result


def band_distribution(records: Sequence[CorpusRecord]) -> Counter:
    counts: Counter = Counter()
    for record in records:
        counts[record.band.value] += 1
    return counts
