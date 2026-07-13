"""Unit coverage for Gemini batch result parsing + state mapping (no network)."""

from __future__ import annotations

import json

from pipeline.llm import gemini_batch


def _line(key, text=None, error=None):
    obj = {"key": key}
    if error is not None:
        obj["error"] = error
    if text is not None:
        obj["response"] = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return json.dumps(obj, ensure_ascii=False)


def test_parse_batch_results_extracts_text_by_key():
    data = ("\n".join([
        _line("12", text='{"entries": []}'),
        _line("7", text='{"entries": [{"han": "天"}]}'),
    ])).encode("utf-8")
    out = gemini_batch.parse_batch_results(data)
    assert out == {"12": '{"entries": []}', "7": '{"entries": [{"han": "天"}]}'}


def test_parse_batch_results_skips_errors_and_junk():
    data = ("\n".join([
        _line("1", error={"code": 429, "message": "quota"}),  # error line → skipped
        "not json at all",                                     # junk → skipped
        "",                                                    # blank → skipped
        _line("2", text="ok"),
    ])).encode("utf-8")
    assert gemini_batch.parse_batch_results(data) == {"2": "ok"}


def test_parse_batch_results_concatenates_multiple_parts():
    obj = {"key": "3", "response": {"candidates": [{"content": {"parts": [
        {"text": "abc"}, {"text": "def"}]}}]}}
    out = gemini_batch.parse_batch_results((json.dumps(obj)).encode("utf-8"))
    assert out == {"3": "abcdef"}


def test_norm_state_maps_job_states():
    assert gemini_batch.norm_state("JOB_STATE_SUCCEEDED") == "succeeded"
    assert gemini_batch.norm_state("JOB_STATE_RUNNING") == "running"
    assert gemini_batch.norm_state("JOB_STATE_FAILED") == "failed"
    assert gemini_batch.norm_state("something weird") == "unknown"
