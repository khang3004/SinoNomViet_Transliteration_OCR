import json

import pytest

from app.core.assist import (
    AssistConfig,
    AssistError,
    build_prompt,
    suggest,
)
from app.core.models import GeminiVerdict, Verdict

GT = "年歲漸長心要活得自由"
CFG = AssistConfig(api_key="test-key", model="test-model")


def draft(**overrides):
    payload = {
        "verdict": "minor",
        "corrected": "年歲漸長心要活得自在",
        "gemini_verdict": "partial",
        "note": "Last character differs.",
    }
    payload.update(overrides)
    return lambda prompt, parts: json.dumps(payload, ensure_ascii=False)


class TestPrompt:
    def test_carries_the_three_inputs(self):
        prompt = build_prompt(GT, "年歲漸長", 0.8)
        assert GT in prompt
        assert "年歲漸長" in prompt
        assert "80.0% agreement" in prompt

    def test_absent_values_are_named_not_blank(self):
        prompt = build_prompt("", "", None)
        assert "(empty)" in prompt and "(no output)" in prompt
        # No percentage is quoted when there is nothing to compare.
        assert "% agreement" not in prompt

    def test_every_verdict_is_offered_to_the_model(self):
        prompt = build_prompt(GT, "x", 0.5)
        for verdict in Verdict:
            assert verdict.value in prompt


class TestSuggest:
    def test_fills_the_whole_form(self):
        result = suggest(
            CFG, image=b"jpeg", mime_type="image/jpeg",
            ground_truth=GT, gemini="年歲漸長", similarity=0.8,
            generate=draft(),
        )
        assert result.verdict is Verdict.MINOR
        assert result.corrected == "年歲漸長心要活得自在"
        assert result.gemini_verdict is GeminiVerdict.PARTIAL
        assert result.note == "Last character differs."
        assert result.model == "test-model"

    def test_correct_adopts_their_text_verbatim(self):
        """Even if the model paraphrases, "correct" means their text is the truth."""
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict="correct", corrected="something else"),
        )
        assert result.verdict is Verdict.CORRECT
        assert result.corrected == GT

    def test_a_wrong_verdict_with_an_unchanged_text_becomes_correct(self):
        """The server refuses that combination, so the draft must not produce it.

        The text is trusted over the label: if nothing actually changed, the
        transcription was right.
        """
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict="wrong", corrected=GT),
        )
        assert result.verdict is Verdict.CORRECT

    def test_whitespace_only_differences_do_not_count_as_a_correction(self):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict="minor", corrected="  " + GT + "\n"),
        )
        assert result.verdict is Verdict.CORRECT

    def test_an_empty_correction_on_a_wrong_verdict_becomes_correct(self):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict="wrong", corrected=""),
        )
        assert result.verdict is Verdict.CORRECT

    @pytest.mark.parametrize("verdict", ["unreadable", "not_an_image"])
    def test_unjudgeable_images_carry_no_transcription(self, verdict):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict=verdict, corrected="something"),
        )
        assert result.verdict is Verdict(verdict)
        assert result.corrected == ""

    def test_an_unknown_verdict_does_not_crash_the_form(self):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(verdict="probably fine"),
        )
        assert result.verdict in set(Verdict)

    def test_an_unknown_gemini_verdict_falls_back_to_skipped(self):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(gemini_verdict="excellent"),
        )
        assert result.gemini_verdict is GeminiVerdict.SKIPPED

    def test_notes_are_collapsed_and_capped(self):
        result = suggest(
            CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
            generate=draft(note="line one\n\n   line two   " + "x" * 500),
        )
        assert "\n" not in result.note
        assert len(result.note) <= 400


class TestFailures:
    def test_no_api_key_says_so(self):
        with pytest.raises(AssistError, match="GEMINI_API_KEY"):
            suggest(AssistConfig(), image=b"j", mime_type="image/jpeg", ground_truth=GT)

    def test_a_missing_image_is_refused(self):
        with pytest.raises(AssistError, match="image could not be loaded"):
            suggest(CFG, image=b"", mime_type="image/jpeg", ground_truth=GT,
                    generate=draft())

    def test_an_oversized_image_is_refused(self):
        cfg = AssistConfig(api_key="k", max_image_bytes=4)
        with pytest.raises(AssistError, match="too large"):
            suggest(cfg, image=b"too long", mime_type="image/jpeg",
                    ground_truth=GT, generate=draft())

    def test_non_json_output_is_reported_with_what_came_back(self):
        with pytest.raises(AssistError, match="not return usable JSON"):
            suggest(CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
                    generate=lambda p, parts: "I think it looks fine!")

    def test_json_that_is_not_an_object_is_refused(self):
        with pytest.raises(AssistError, match="not an object"):
            suggest(CFG, image=b"j", mime_type="image/jpeg", ground_truth=GT,
                    generate=lambda p, parts: "[1, 2, 3]")

    def test_the_image_reaches_the_model(self):
        seen = {}

        def spy(prompt, parts):
            seen["parts"] = parts
            return json.dumps({"verdict": "correct", "corrected": GT,
                               "gemini_verdict": "good", "note": ""})

        suggest(CFG, image=b"\xff\xd8jpegbytes", mime_type="image/jpeg",
                ground_truth=GT, generate=spy)
        assert seen["parts"] == [b"\xff\xd8jpegbytes"]
