"""OCR result parsing for both PaddleOCR API generations.

Both are supported because the PP-OCRv6 upgrade is gated on a benchmark that may
send us back to 2.8.1:

    3.x  engine.predict(ndarray) -> [{rec_texts, rec_scores, ...}]
    2.x  engine.ocr(ndarray)     -> [[[box, (text, score)], ...]]
"""

from __future__ import annotations

import pytest

from app.core.ocr import parse_ocr_result, parse_predict_result


class TestPredictApi:
    def test_plain_dict(self):
        raw = [{"rec_texts": ["平定營", "abc"], "rec_scores": [0.97, 0.12]}]
        assert parse_predict_result(raw) == [("平定營", 0.97), ("abc", 0.12)]

    def test_single_result_not_in_a_list(self):
        assert parse_predict_result({"rec_texts": ["文字"], "rec_scores": [0.8]}) == [
            ("文字", 0.8)
        ]

    def test_object_exposing_json_attribute(self):
        class Result:
            json = {"rec_texts": ["公堂"], "rec_scores": [0.9]}

        assert parse_predict_result([Result()]) == [("公堂", 0.9)]

    def test_payload_nested_under_res(self):
        class Result:
            json = {"res": {"rec_texts": ["官"], "rec_scores": [0.7]}}

        assert parse_predict_result([Result()]) == [("官", 0.7)]

    def test_missing_scores_default_to_confident(self):
        # Better to keep the text than discard a detection over a missing score.
        assert parse_predict_result([{"rec_texts": ["a", "b"], "rec_scores": [0.5]}]) == [
            ("a", 0.5),
            ("b", 1.0),
        ]

    def test_empty_texts_are_dropped(self):
        raw = [{"rec_texts": ["", "文"], "rec_scores": [0.9, 0.9]}]
        assert parse_predict_result(raw) == [("文", 0.9)]

    @pytest.mark.parametrize("raw", [None, [], [None], [{}], ["nonsense"]])
    def test_degenerate_inputs_never_raise(self, raw):
        assert parse_predict_result(raw) == []


class TestOcrApi:
    def test_standard_shape(self):
        raw = [[[[[0, 0]], ("平定", 0.95)], [[[1, 1]], ("xy", 0.2)]]]
        assert parse_ocr_result(raw) == [("平定", 0.95), ("xy", 0.2)]

    @pytest.mark.parametrize("raw", [None, [], [None], [[]]])
    def test_degenerate_inputs_never_raise(self, raw):
        assert parse_ocr_result(raw) == []

    def test_malformed_entries_are_skipped_not_fatal(self):
        raw = [[["bad"], [[[0, 0]], ("好", 0.9)]]]
        assert parse_ocr_result(raw) == [("好", 0.9)]
