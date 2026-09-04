"""Drafting a review with Gemini, for a human to correct.

The reviewer presses **Prefill** and a model reads the image alongside the
upstream team's transcription, then proposes the whole form: the corrected
text, a verdict, a rating of the upstream Gemini output, and a short note.

**This is a draft, never a review.** The point of this app is that a person
looks at the picture, and a model agreeing with another model is not an audit.
The draft fills the form; the reviewer still has to look and press save.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.core.models import GeminiVerdict, Verdict

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"

# Kept small and closed: the client maps these straight onto the form's controls,
# so an unexpected value would silently leave a control unset.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [v.value for v in Verdict],
        },
        "corrected": {"type": "string"},
        "gemini_verdict": {
            "type": "string",
            "enum": [g.value for g in GeminiVerdict],
        },
        "note": {"type": "string"},
    },
    "required": ["verdict", "corrected", "gemini_verdict", "note"],
}

PROMPT = """\
You are assisting a human reviewer auditing Hán-Nôm (Chinese/Vietnamese \
calligraphic) transcriptions. Your output is a DRAFT that the reviewer will \
check against the image and correct. Do not guess confidently when the image is \
unclear — say so instead.

You are given:
1. The photograph of the calligraphy.
2. THEIR GROUND TRUTH — a transcription made by another team. This is what is \
being audited.
3. GEMINI OCR — a machine transcription, with its character-level agreement \
with their ground truth.

Read the characters in the image yourself. Then judge whether THEIR GROUND \
TRUTH matches what the image actually says.

verdict:
  correct       their transcription matches the image
  minor         small errors — a variant form, one stray or missing character
  wrong         substantially wrong
  unreadable    the image is too damaged, blurred or cropped to judge
  not_an_image  not a photograph of calligraphy at all

corrected: the transcription you believe is correct, read from the image.
  For "correct", repeat their ground truth verbatim.
  For "minor" and "wrong", give the corrected text — it MUST differ from their
  ground truth, otherwise the verdict is "correct".
  For "unreadable" and "not_an_image", return an empty string.

gemini_verdict: how good the GEMINI OCR line is against the image —
  "good", "partial", "bad", or "skipped" if there is no Gemini output.

note: one or two short lines in English saying what differs and why, e.g.
  which characters, or what makes the image hard. Empty if there is nothing
  worth saying.

THEIR GROUND TRUTH:
{ground_truth}

GEMINI OCR{similarity}:
{gemini}
"""


class AssistError(RuntimeError):
    """The draft could not be produced; shown to the reviewer as-is."""


@dataclass(frozen=True)
class AssistConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout_s: float = 60.0
    max_image_bytes: int = 8 * 1024 * 1024

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class Suggestion:
    verdict: Verdict
    corrected: str
    gemini_verdict: GeminiVerdict
    note: str
    model: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "corrected": self.corrected,
            "gemini_verdict": self.gemini_verdict.value,
            "note": self.note,
            "model": self.model,
        }


def _coerce(payload: dict[str, Any], ground_truth: str, model: str) -> Suggestion:
    """Turn the model's JSON into a Suggestion, repairing what can be repaired.

    A draft that half-fills the form is worse than one that is internally
    consistent, so contradictions are resolved here rather than left for the
    reviewer to trip over.
    """
    try:
        verdict = Verdict(str(payload.get("verdict", "")).strip().lower())
    except ValueError:
        verdict = Verdict.MINOR

    try:
        gemini_verdict = GeminiVerdict(
            str(payload.get("gemini_verdict", "skipped")).strip().lower()
        )
    except ValueError:
        gemini_verdict = GeminiVerdict.SKIPPED

    corrected = str(payload.get("corrected", "") or "").strip()
    note = " ".join(str(payload.get("note", "") or "").split())

    if verdict is Verdict.CORRECT:
        # The audited truth IS their text when the verdict is "correct".
        corrected = ground_truth
    elif verdict in (Verdict.MINOR, Verdict.WRONG):
        from app.core.metrics import normalize

        # The server refuses these verdicts with an unchanged correction, so a
        # draft that contradicts itself would be unsavable. Trust the text over
        # the label: if it did not actually change anything, it means "correct".
        if not corrected or normalize(corrected) == normalize(ground_truth):
            verdict = Verdict.CORRECT
            corrected = ground_truth
    else:
        corrected = ""

    return Suggestion(
        verdict=verdict,
        corrected=corrected,
        gemini_verdict=gemini_verdict,
        note=note[:400],
        model=model,
    )


def build_prompt(ground_truth: str, gemini: str, similarity: float | None) -> str:
    percent = ""
    if similarity is not None:
        percent = f" ({similarity * 100:.1f}% agreement with their ground truth)"
    return PROMPT.format(
        ground_truth=ground_truth or "(empty)",
        gemini=gemini or "(no output)",
        similarity=percent,
    )


def suggest(
    cfg: AssistConfig,
    *,
    image: bytes,
    mime_type: str,
    ground_truth: str,
    gemini: str = "",
    similarity: float | None = None,
    generate: Callable[[str, list[Any]], str] | None = None,
) -> Suggestion:
    """Ask the model for a draft review. Blocking — call it off the event loop.

    ``generate`` exists so the tests can exercise the prompt, the parsing and
    the repair rules without a network call or an API key.
    """
    if generate is None and not cfg.configured:
        raise AssistError(
            "GEMINI_API_KEY is not set, so Prefill cannot draft a review."
        )
    if not image:
        raise AssistError("The image could not be loaded, so there is nothing to read.")
    if len(image) > cfg.max_image_bytes:
        raise AssistError(f"The image is {len(image)} bytes, too large to send.")

    prompt = build_prompt(ground_truth, gemini, similarity)

    if generate is not None:
        raw = generate(prompt, [image])
    else:
        raw = _generate(cfg, prompt, image, mime_type)

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AssistError(
            f"The model did not return usable JSON: {str(raw)[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssistError("The model returned JSON that is not an object.")

    return _coerce(payload, ground_truth, cfg.model)


def _generate(cfg: AssistConfig, prompt: str, image: bytes, mime_type: str) -> str:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:  # pragma: no cover - pinned dependency
        raise AssistError(
            "The 'google-genai' package is not installed in this image."
        ) from exc

    client = genai.Client(api_key=cfg.api_key)
    try:
        response = client.models.generate_content(
            model=cfg.model,
            contents=[
                types.Part.from_bytes(data=image, mime_type=mime_type or "image/jpeg"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                # Low but not zero: this is a reading task, not a creative one.
                temperature=0.2,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the reviewer verbatim
        raise AssistError(
            f"{type(exc).__name__}: {exc}. If this names the model, set "
            f"GEMINI_MODEL to one your key can use (currently {cfg.model!r})."
        ) from exc

    text = getattr(response, "text", "") or ""
    if not text.strip():
        raise AssistError("The model returned an empty response.")
    return text
