"""The shapes that move between modules.

One image is one unit of work. The upstream team ships one row per image, so a
post with three photographs is three reviewable records that happen to share a
``post_id`` — which is why the per-post cap in the sampler exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = "ocr_audit/1.0"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Band(str, Enum):
    """How far the upstream Gemini output sits from their own ground truth.

    The sampler stratifies on this. An audit that draws uniformly spends most of
    its effort on rows where model and label already agree, which is precisely
    where there is nothing to learn.
    """

    EXACT = "exact"      # normalized identical
    NEAR = "near"        # >= 90% character accuracy
    FAR = "far"          # 50-90%
    POOR = "poor"        # < 50%
    EMPTY = "empty"      # one side blank — nothing to compare

    @property
    def label(self) -> str:
        return {
            "exact": "Identical",
            "near": "Near match",
            "far": "Diverging",
            "poor": "Far apart",
            "empty": "One side empty",
        }[self.value]


class Verdict(str, Enum):
    """The reviewer's judgement on the upstream team's ``ground_truth``.

    This is an audit of their label, not of the model: the question on screen is
    "does this transcription match the image", and the answer is what makes
    every downstream accuracy figure trustworthy.
    """

    CORRECT = "correct"          # matches the image
    MINOR = "minor"              # small errors — variant forms, a stray char
    WRONG = "wrong"              # substantially wrong
    UNREADABLE = "unreadable"    # image too damaged or unclear to judge
    NOT_AN_IMAGE = "not_an_image"  # broken, missing, or not a photograph

    @property
    def counts_as_reviewed(self) -> bool:
        """Whether this verdict consumes a slot in the sample.

        ``NOT_AN_IMAGE`` does not: the record was never reviewable, so it is
        returned to the pool and the reviewer is owed a replacement rather than
        being credited for it.
        """
        return self is not Verdict.NOT_AN_IMAGE

    @property
    def ground_truth_ok(self) -> bool:
        return self in (Verdict.CORRECT, Verdict.MINOR)


class GeminiVerdict(str, Enum):
    """Optional, lighter judgement on the Gemini output shown alongside."""

    GOOD = "good"
    PARTIAL = "partial"
    BAD = "bad"
    SKIPPED = "skipped"


@dataclass
class CorpusRecord:
    """One image awaiting audit."""

    record_id: str
    post_id: str
    idx: int
    image: str
    suffix: str = ".jpg"
    caption: str = ""
    ground_truth: str = ""
    label: str = ""
    gemini: str = ""
    deepseek: str = ""
    post_link: str = ""
    band: Band = Band.EMPTY
    gemini_similarity: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "post_id": self.post_id,
            "idx": self.idx,
            "image": self.image,
            "suffix": self.suffix,
            "caption": self.caption,
            "ground_truth": self.ground_truth,
            "label": self.label,
            "gemini": self.gemini,
            "deepseek": self.deepseek,
            "post_link": self.post_link,
            "band": self.band.value,
            "gemini_similarity": self.gemini_similarity,
            "sources": self.sources,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CorpusRecord":
        return cls(
            record_id=data["record_id"],
            post_id=data.get("post_id", ""),
            idx=int(data.get("idx", 0)),
            image=data.get("image", ""),
            suffix=data.get("suffix", ".jpg"),
            caption=data.get("caption", ""),
            ground_truth=data.get("ground_truth", ""),
            label=data.get("label", ""),
            gemini=data.get("gemini", ""),
            deepseek=data.get("deepseek", ""),
            post_link=data.get("post_link", ""),
            band=Band(data.get("band", "empty")),
            gemini_similarity=data.get("gemini_similarity") or {},
            sources=data.get("sources") or [],
        )


@dataclass
class Review:
    """One reviewer's audit of one record. Append-only; the latest wins."""

    record_id: str
    username: str
    verdict: Verdict
    corrected: str = ""
    note: str = ""
    gemini_verdict: GeminiVerdict = GeminiVerdict.SKIPPED
    # Filled in by the review store, not the client.
    ground_truth_similarity: dict[str, Any] = field(default_factory=dict)
    gemini_vs_corrected: dict[str, Any] = field(default_factory=dict)
    reviewed_at: str = field(default_factory=utcnow_iso)
    seconds_spent: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "username": self.username,
            "verdict": self.verdict.value,
            "corrected": self.corrected,
            "note": self.note,
            "gemini_verdict": self.gemini_verdict.value,
            "ground_truth_similarity": self.ground_truth_similarity,
            "gemini_vs_corrected": self.gemini_vs_corrected,
            "reviewed_at": self.reviewed_at,
            "seconds_spent": self.seconds_spent,
            "schema_version": SCHEMA_VERSION,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Review":
        return cls(
            record_id=data["record_id"],
            username=data.get("username", ""),
            verdict=Verdict(data.get("verdict", "correct")),
            corrected=data.get("corrected", ""),
            note=data.get("note", ""),
            gemini_verdict=GeminiVerdict(data.get("gemini_verdict", "skipped")),
            ground_truth_similarity=data.get("ground_truth_similarity") or {},
            gemini_vs_corrected=data.get("gemini_vs_corrected") or {},
            reviewed_at=data.get("reviewed_at", ""),
            seconds_spent=float(data.get("seconds_spent", 0.0) or 0.0),
        )


@dataclass
class Assignment:
    """A claim on a record by one reviewer. Append-only; the latest wins.

    ``released`` marks a record handed back — because the image turned out to be
    unusable, or an admin reassigned it. A released record returns to the pool.
    """

    record_id: str
    username: str
    band: Band
    assigned_at: str = field(default_factory=utcnow_iso)
    released: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "username": self.username,
            "band": self.band.value,
            "assigned_at": self.assigned_at,
            "released": self.released,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Assignment":
        return cls(
            record_id=data["record_id"],
            username=data.get("username", ""),
            band=Band(data.get("band", "empty")),
            assigned_at=data.get("assigned_at", ""),
            released=bool(data.get("released", False)),
        )
