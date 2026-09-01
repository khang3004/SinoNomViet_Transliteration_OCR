"""The audit itself: what exists, who holds it, and what they decided.

Three logs, one purpose:

* ``corpus/records.jsonl`` — a snapshot of the merged upstream data. Replaced
  wholesale when new files are uploaded, never appended to.
* ``sample.jsonl`` — which of those ~9,000 records are in the study. Reviewers
  are only ever assigned from inside it, and it is the denominator of every
  progress bar (see ``app.core.sample``).
* ``assignments.jsonl`` — append-only claims. A record belongs to exactly one
  reviewer; releasing it writes a new row rather than deleting the old one.
* ``reviews.jsonl`` — append-only verdicts. A reviewer changing their mind adds
  a row; the fold keeps the latest and the history stays on disk.

The rule that shapes everything here is **no overlap**: two reviewers never hold
the same image. That makes the per-post cap a global count, makes "available"
mean "claimed by nobody", and means a reviewer who hits an unusable image is
owed a replacement rather than being told to skip it.
"""

from __future__ import annotations

import logging
import random
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.core import sampling
from app.core.corpus import IngestReport
from app.core.jsonlog import JsonlLog, read_json, write_json, write_jsonl
from app.core.metrics import compare
from app.core.models import (
    Assignment,
    Band,
    CorpusRecord,
    GeminiVerdict,
    Review,
    Verdict,
)
from app.core.sample import SampleDraw, SampleStore

log = logging.getLogger(__name__)


class AuditError(ValueError):
    """A request the caller can correct — bad verdict, record already taken."""


class CorpusStore:
    """The merged upstream data, as a replaceable snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.records_path = self.root / "records.jsonl"
        self.report_path = self.root / "ingest.json"
        self._cache: list[CorpusRecord] | None = None
        self._index: dict[str, CorpusRecord] | None = None
        self._signature: tuple[float, int] | None = None

    def replace(self, records: list[CorpusRecord], report: IngestReport) -> None:
        write_jsonl(self.records_path, [r.to_json() for r in records])
        write_json(self.report_path, report.to_json())
        self._cache = self._index = self._signature = None
        log.info("corpus replaced: %d records", len(records))

    def _signature_now(self) -> tuple[float, int] | None:
        try:
            info = self.records_path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def records(self) -> list[CorpusRecord]:
        signature = self._signature_now()
        if signature is None:
            self._cache, self._index = [], {}
            return []
        if self._cache is not None and self._signature == signature:
            return self._cache

        log_file = JsonlLog(self.records_path)
        records = [CorpusRecord.from_json(row) for row in log_file.rows()]
        self._cache = records
        self._index = {r.record_id: r for r in records}
        self._signature = signature
        return records

    def get(self, record_id: str) -> CorpusRecord | None:
        self.records()
        return (self._index or {}).get(record_id)

    def report(self) -> dict[str, Any]:
        return read_json(self.report_path, default={}) or {}

    def band_counts(self) -> Counter:
        return sampling.band_distribution(self.records())

    def post_count(self) -> int:
        return len({r.post_id for r in self.records()})


@dataclass
class QueueItem:
    """One row of a reviewer's work list, joined with its review if any."""

    record: CorpusRecord
    assignment: Assignment
    review: Review | None = None

    @property
    def done(self) -> bool:
        return self.review is not None and self.review.verdict.counts_as_reviewed

    def to_json(self, image_url: str = "") -> dict[str, Any]:
        data = self.record.to_json()
        data.update(
            {
                "image_url": image_url,
                "assigned_at": self.assignment.assigned_at,
                "assigned_to": self.assignment.username,
                "band_label": self.record.band.label,
                "done": self.done,
                "review": self.review.to_json() if self.review else None,
            }
        )
        return data


class AuditStore:
    """Assignment and review state over a corpus snapshot."""

    def __init__(
        self,
        data_dir: Path,
        eligible: Callable[[CorpusRecord], bool] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.corpus = CorpusStore(self.data_dir / "corpus")
        self.sample = SampleStore(self.data_dir)
        # Whether a record can actually be shown. Injected rather than imported
        # so core stays free of the Drive layer. Without it every record counts
        # as eligible, which is what the tests and the CLI want.
        self.eligible = eligible
        self.assignments = JsonlLog(self.data_dir / "assignments.jsonl")
        self.reviews = JsonlLog(self.data_dir / "reviews.jsonl")
        # Assignment must be serialised: two reviewers clicking "get more" at
        # the same moment could otherwise be handed the same record.
        self._draw_lock = threading.Lock()

    # --- folded state ----------------------------------------------------

    def assignment_state(self) -> dict[str, Assignment]:
        folded = self.assignments.fold(lambda row: row["record_id"])
        return {rid: Assignment.from_json(row) for rid, row in folded.items()}

    def review_state(self) -> dict[str, Review]:
        folded = self.reviews.fold(lambda row: row["record_id"])
        return {rid: Review.from_json(row) for rid, row in folded.items()}

    def active_assignments(self) -> dict[str, Assignment]:
        return {
            rid: a for rid, a in self.assignment_state().items() if not a.released
        }

    def records_by_id(self) -> dict[str, CorpusRecord]:
        return {r.record_id: r for r in self.corpus.records()}

    def drawable(self) -> list[CorpusRecord]:
        """Corpus records whose image can actually be displayed.

        A record whose image is missing from the Drive index would reach a
        reviewer as a broken thumbnail, waste their time, and then be flagged
        and replaced. Excluding it from the draw is both cheaper and honest:
        the study is built only from images the app can show.
        """
        records = self.corpus.records()
        if self.eligible is None:
            return records
        return [r for r in records if self.eligible(r)]

    def sample_records(self) -> list[CorpusRecord]:
        """The study, in corpus order — the ~500 images this audit is about."""
        members = self.sample.members()
        return [r for r in self.corpus.records() if r.record_id in members]

    def available(self) -> list[CorpusRecord]:
        """Study records nobody holds.

        Scoped to the sample, not the corpus: a reviewer must never be handed
        one of the other 8,500 images, because nothing outside the study counts
        towards it.
        """
        held = set(self.active_assignments())
        return [r for r in self.sample_records() if r.record_id not in held]

    # --- the study sample ------------------------------------------------

    def create_sample(
        self,
        size: int,
        *,
        targets: dict[Band, float] | None = None,
        per_post_cap: int = sampling.DEFAULT_PER_POST_CAP,
        created_by: str = "",
        rng: random.Random | None = None,
    ) -> SampleDraw:
        """Draw the study: ``size`` images, stratified and capped per post.

        Destructive by design — it discards the previous membership. Reviews are
        never touched, so re-drawing after a corrective re-ingest keeps every
        verdict already recorded for records that stay in.
        """
        if size <= 0:
            raise AuditError("A study needs at least one image.")
        if not self.corpus.records():
            raise AuditError("Load the upstream data before drawing a sample.")
        records = self.drawable()
        if not records:
            raise AuditError(
                "None of the loaded records have an image in the Drive index. "
                "Index the Drive folder first, and check the filenames match."
            )

        with self._draw_lock:
            self.sample.reset(
                size,
                sampling.normalize_targets(targets),
                per_post_cap,
                created_by=created_by,
            )
            result = self.sample.draw_into(records, count=size, rng=rng)
        log.info("study sample drawn: %d of %d requested", len(result.added), size)
        return result

    def top_up_sample(self, rng: random.Random | None = None) -> SampleDraw:
        """Refill the study to its target size after drops.

        Called automatically when a reviewer flags an image as unusable: the
        study is meant to yield ``size`` judged images, so a broken one is
        replaced rather than quietly shrinking the denominator.
        """
        if not self.sample.exists:
            return SampleDraw([], 0, Counter(), Counter())
        deficit = self.sample.size - len(self.sample.members())
        if deficit <= 0:
            return SampleDraw([], 0, Counter(), Counter())
        with self._draw_lock:
            return self.sample.draw_into(
                self.drawable(), count=deficit, rng=rng, reason="replacement"
            )

    # --- assignment ------------------------------------------------------

    def assign(
        self,
        username: str,
        count: int,
        *,
        targets: dict[Band, float] | None = None,
        rng: random.Random | None = None,
    ) -> sampling.SampleResult:
        """Draw and claim ``count`` study records for one reviewer.

        The lock spans draw *and* write. Drawing outside it would let two
        concurrent requests select the same record and both believe they own it.

        The per-post cap is NOT re-applied here: it was already enforced when
        the study was drawn, and applying it again would block a reviewer from
        legitimately holding both images of a post that the study admitted.
        """
        if count <= 0:
            raise AuditError("Ask for at least one image.")
        if not self.sample.exists:
            raise AuditError(
                "No study sample has been drawn yet — an admin needs to draw one "
                "before reviewing can start."
            )
        with self._draw_lock:
            result = sampling.draw(
                self.available(),
                n=count,
                targets=targets or self.sample.targets(),
                per_post_cap=0,
                rng=rng,
            )
            if result.records:
                self.assignments.extend(
                    [
                        Assignment(
                            record_id=r.record_id, username=username, band=r.band
                        ).to_json()
                        for r in result.records
                    ]
                )
        log.info(
            "assigned %d/%d records to %r", len(result.records), count, username
        )
        return result

    def release(self, record_id: str, username: str) -> None:
        """Hand a record back to the pool."""
        current = self.assignment_state().get(record_id)
        if current is None or current.released:
            return
        self.assignments.append(
            Assignment(
                record_id=record_id,
                username=username,
                band=current.band,
                released=True,
            ).to_json()
        )

    # --- reviewing -------------------------------------------------------

    def submit(
        self,
        username: str,
        record_id: str,
        verdict: Verdict,
        *,
        corrected: str = "",
        note: str = "",
        gemini_verdict: GeminiVerdict = GeminiVerdict.SKIPPED,
        seconds_spent: float = 0.0,
        is_admin: bool = False,
    ) -> Review:
        record = self.corpus.get(record_id)
        if record is None:
            raise AuditError(f"Unknown record: {record_id}")

        holder = self.active_assignments().get(record_id)
        if holder is None:
            raise AuditError("That image is not currently assigned to anyone.")
        if holder.username != username and not is_admin:
            raise AuditError(f"That image is assigned to {holder.username}.")

        corrected = (corrected or "").strip()
        if verdict is Verdict.CORRECT:
            # The reviewer asserted the upstream transcription matches the image,
            # so it IS the corrected text. Filling this server-side is what keeps
            # a pre-populated textbox from turning "not looked at" into "100%".
            corrected = corrected or record.ground_truth
        elif verdict in (Verdict.MINOR, Verdict.WRONG):
            if not corrected:
                raise AuditError(
                    "Type the correct transcription when marking a label wrong."
                )
        else:
            corrected = ""

        review = Review(
            record_id=record_id,
            username=username,
            verdict=verdict,
            corrected=corrected,
            note=(note or "").strip(),
            gemini_verdict=gemini_verdict,
            seconds_spent=max(0.0, float(seconds_spent or 0.0)),
        )

        if corrected:
            # The reviewer's text is the reference now — that is the whole point
            # of the audit. Their ground truth and Gemini are both hypotheses
            # measured against it.
            review.ground_truth_similarity = compare(
                corrected, record.ground_truth
            ).to_json()
            review.gemini_vs_corrected = compare(corrected, record.gemini).to_json()

        self.reviews.append(review.to_json())

        # An unusable image was never reviewable work. Return it, drop it from
        # the study so it cannot be handed to the next reviewer, and pull a
        # replacement in the same band — the study is meant to yield `size`
        # judged images, not `size` minus however many were broken.
        if verdict is Verdict.NOT_AN_IMAGE:
            self.release(record_id, username)
            if self.sample.exists and record_id in self.sample.members():
                self.sample.drop(record_id, record.band, reason="flagged_unusable")
                self.top_up_sample()

        return review

    # --- views -----------------------------------------------------------

    def queue(self, username: str, *, include_done: bool = True) -> list[QueueItem]:
        """One reviewer's work, oldest assignment first."""
        reviews = self.review_state()
        items: list[QueueItem] = []
        for record_id, assignment in self.active_assignments().items():
            if assignment.username != username:
                continue
            record = self.corpus.get(record_id)
            if record is None:
                continue
            item = QueueItem(record, assignment, reviews.get(record_id))
            if item.done and not include_done:
                continue
            items.append(item)
        items.sort(key=lambda i: (i.done, i.assignment.assigned_at, i.record.record_id))
        return items

    def all_reviewed(self, reviewers: Iterable[str] | None = None) -> list[QueueItem]:
        """Every completed review, newest first — the team's shared view."""
        wanted = set(reviewers) if reviewers else None
        assignments = self.assignment_state()
        items: list[QueueItem] = []
        for record_id, review in self.review_state().items():
            if wanted is not None and review.username not in wanted:
                continue
            record = self.corpus.get(record_id)
            assignment = assignments.get(record_id)
            if record is None or assignment is None:
                continue
            items.append(QueueItem(record, assignment, review))
        items.sort(key=lambda i: i.review.reviewed_at if i.review else "", reverse=True)
        return items

    # --- progress --------------------------------------------------------

    def progress(self, targets: dict[Band, float] | None = None) -> dict[str, Any]:
        """The panel: study depth, claimed, reviewed, per band and overall.

        The denominator is the **study sample**, not the corpus and not what
        reviewers happen to have claimed. "62% done" means 62% of the 500 images
        this audit set out to judge.
        """
        by_id = self.records_by_id()
        in_study = self.sample_records()
        size = self.sample.size or len(in_study)
        shares = targets or self.sample.targets()

        plan = sampling.quotas(size, shares) if size else {}
        study_bands = sampling.band_distribution(in_study)
        assigned_bands: Counter = Counter()
        reviewed_bands: Counter = Counter()

        for record_id in self.active_assignments():
            record = by_id.get(record_id)
            if record is not None:
                assigned_bands[record.band.value] += 1

        reviews = self.review_state()
        members = self.sample.members()
        verdicts: Counter = Counter()
        gemini_verdicts: Counter = Counter()
        gt_accuracy: list[float] = []
        gemini_accuracy: list[float] = []

        for record_id, review in reviews.items():
            verdicts[review.verdict.value] += 1
            if not review.verdict.counts_as_reviewed:
                continue
            # Only the study counts. A review of a record later dropped from the
            # sample stays on disk but must not inflate the study's progress.
            if record_id not in members:
                continue
            record = by_id.get(record_id)
            if record is not None:
                reviewed_bands[record.band.value] += 1
            if review.gemini_verdict is not GeminiVerdict.SKIPPED:
                gemini_verdicts[review.gemini_verdict.value] += 1
            if review.ground_truth_similarity:
                gt_accuracy.append(
                    float(review.ground_truth_similarity.get("cer_accuracy", 0.0))
                )
            if review.gemini_vs_corrected:
                gemini_accuracy.append(
                    float(review.gemini_vs_corrected.get("cer_accuracy", 0.0))
                )

        assigned_total = sum(assigned_bands.values())
        reviewed_total = sum(reviewed_bands.values())

        bands = []
        for band in Band:
            target = plan.get(band, 0)
            in_sample = study_bands.get(band.value, 0)
            assigned = assigned_bands.get(band.value, 0)
            reviewed = reviewed_bands.get(band.value, 0)
            bands.append(
                {
                    "band": band.value,
                    "label": band.label,
                    "target_share": round(
                        sampling.normalize_targets(shares).get(band, 0.0), 4
                    ),
                    "target": target,
                    "in_sample": in_sample,
                    "in_corpus": self.corpus.band_counts().get(band.value, 0),
                    "assigned": assigned,
                    "reviewed": reviewed,
                    "remaining": max(0, in_sample - reviewed),
                    "unclaimed": max(0, in_sample - assigned),
                    "percent": round(reviewed / in_sample * 100, 1) if in_sample else 0.0,
                }
            )

        return {
            "corpus": {
                "records": len(self.corpus.records()),
                "drawable": len(self.drawable()),
                "posts": self.corpus.post_count(),
                "ingest": self.corpus.report(),
            },
            "sample": self.sample.status(by_id),
            "target": size,
            "assigned": assigned_total,
            "reviewed": reviewed_total,
            "unclaimed": max(0, len(in_study) - assigned_total),
            "pending": max(0, assigned_total - reviewed_total),
            # The team progress bar: reviewed out of the study, full stop.
            "percent": round(reviewed_total / size * 100, 1) if size else 0.0,
            "sampled_percent": round(size / len(self.corpus.records()) * 100, 2)
            if self.corpus.records()
            else 0.0,
            "bands": bands,
            "verdicts": dict(verdicts),
            "gemini_verdicts": dict(gemini_verdicts),
            # The headline numbers this whole app exists to produce.
            "ground_truth_accuracy": round(
                sum(gt_accuracy) / len(gt_accuracy), 4
            ) if gt_accuracy else None,
            "gemini_accuracy": round(
                sum(gemini_accuracy) / len(gemini_accuracy), 4
            ) if gemini_accuracy else None,
            "audited_pairs": len(gemini_accuracy),
        }

    def per_reviewer(self) -> list[dict[str, Any]]:
        """Who has what, and how far along — the team panel."""
        assigned: Counter = Counter()
        for assignment in self.active_assignments().values():
            assigned[assignment.username] += 1

        done: Counter = Counter()
        flagged: Counter = Counter()
        seconds: dict[str, float] = defaultdict(float)
        last: dict[str, str] = {}
        accuracy: dict[str, list[float]] = defaultdict(list)

        for review in self.review_state().values():
            name = review.username
            if not review.verdict.counts_as_reviewed:
                flagged[name] += 1
                continue
            done[name] += 1
            seconds[name] += review.seconds_spent
            if review.reviewed_at > last.get(name, ""):
                last[name] = review.reviewed_at
            if review.ground_truth_similarity:
                accuracy[name].append(
                    float(review.ground_truth_similarity.get("cer_accuracy", 0.0))
                )

        names = set(assigned) | set(done) | set(flagged)
        rows = []
        for name in sorted(names):
            scores = accuracy.get(name, [])
            rows.append(
                {
                    "username": name,
                    "assigned": assigned.get(name, 0),
                    "reviewed": done.get(name, 0),
                    "remaining": max(0, assigned.get(name, 0) - done.get(name, 0)),
                    "flagged_unusable": flagged.get(name, 0),
                    "avg_seconds": round(seconds[name] / done[name], 1)
                    if done.get(name)
                    else None,
                    "last_review_at": last.get(name, ""),
                    "ground_truth_accuracy": round(sum(scores) / len(scores), 4)
                    if scores
                    else None,
                }
            )
        return rows

    # --- export ----------------------------------------------------------

    def export_rows(self) -> list[dict[str, Any]]:
        """One row per reviewed record, in the upstream team's column order."""
        rows = []
        for item in self.all_reviewed():
            record, review = item.record, item.review
            if review is None:
                continue
            gt_sim = review.ground_truth_similarity or {}
            gm_sim = review.gemini_vs_corrected or {}
            rows.append(
                {
                    "post_id": record.post_id,
                    "image": record.image,
                    "caption": record.caption,
                    "ground_truth": record.ground_truth,
                    "gemini_ocr": record.gemini,
                    "deepseek_ocr": record.deepseek,
                    "post_link": record.post_link,
                    "band": record.band.value,
                    "verdict": review.verdict.value,
                    "corrected": review.corrected,
                    "note": review.note,
                    "gemini_verdict": review.gemini_verdict.value,
                    "ground_truth_cer_accuracy": gt_sim.get("cer_accuracy"),
                    "ground_truth_max_accuracy": gt_sim.get("max_accuracy"),
                    "gemini_cer_accuracy": gm_sim.get("cer_accuracy"),
                    "gemini_max_accuracy": gm_sim.get("max_accuracy"),
                    "reviewer": review.username,
                    "reviewed_at": review.reviewed_at,
                    "seconds_spent": review.seconds_spent,
                }
            )
        return rows
