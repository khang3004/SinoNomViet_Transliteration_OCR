"""tests/test_layout.py
====================
Unit tests for spatial_layout_engine.py

Tests verify the Adaptive Horizontal Threshold clustering algorithm
produces correct Right-to-Left, Top-to-Bottom reading order.
"""

from __future__ import annotations

import pytest

from sinonom_ocr.spatial_layout_engine import (
    BoundingBox,
    create_mock_multi_char_response,
    create_mock_ocr_response,
    process_page_layout,
)


class TestBoundingBox:
    """Unit tests for BoundingBox geometry calculations."""

    def test_from_xyxy_centroid(self) -> None:
        """Centroid is computed correctly from axis-aligned coordinates."""
        bb = BoundingBox.from_xyxy(100, 50, 200, 150)
        assert bb.cx == pytest.approx(150.0)
        assert bb.cy == pytest.approx(100.0)

    def test_from_xyxy_dimensions(self) -> None:
        """Width and height are derived correctly."""
        bb = BoundingBox.from_xyxy(10, 20, 60, 80)
        assert bb.width == pytest.approx(50.0)
        assert bb.height == pytest.approx(60.0)

    def test_polygon_preserved(self) -> None:
        """Raw polygon is preserved on the instance."""
        poly = [(0, 0), (10, 0), (10, 20), (0, 20)]
        bb = BoundingBox(raw_polygon=poly, text="百")
        assert bb.raw_polygon == poly


class TestAdaptiveHorizontalThreshold:
    """Tests for the AHT column-clustering algorithm."""

    def test_five_column_canonical(self) -> None:
        """The canonical 5-column example produces 5 distinct columns."""
        boxes = create_mock_ocr_response()
        columns, ordered = process_page_layout(boxes)
        assert len(columns) == 5

    def test_reading_order_right_to_left(self) -> None:
        """Column 0 (rightmost) has greater x-center than Column 1."""
        boxes = create_mock_ocr_response()
        columns, _ = process_page_layout(boxes)
        for i in range(len(columns) - 1):
            assert columns[i].x_center > columns[i + 1].x_center, (
                f"Column {i} (x={columns[i].x_center:.0f}) should be right of "
                f"column {i + 1} (x={columns[i + 1].x_center:.0f})"
            )

    def test_reading_index_contiguous(self) -> None:
        """reading_idx values form a contiguous 0-based sequence."""
        boxes = create_mock_ocr_response()
        _, ordered = process_page_layout(boxes)
        indices = [b.reading_idx for b in ordered]
        assert indices == list(range(len(boxes)))

    def test_multi_char_column_count(self) -> None:
        """Multi-character mock produces exactly 3 columns."""
        boxes = create_mock_multi_char_response()
        columns, _ = process_page_layout(boxes)
        assert len(columns) == 3

    def test_top_to_bottom_within_column(self) -> None:
        """Boxes within each column are sorted top-to-bottom (ascending cy)."""
        boxes = create_mock_multi_char_response()
        columns, _ = process_page_layout(boxes)
        for col in columns:
            cys = [b.cy for b in col.boxes]
            assert cys == sorted(cys), (
                f"Column {col.column_id} boxes not sorted top-to-bottom: {cys}"
            )

    def test_empty_input_returns_empty(self) -> None:
        """Passing an empty list returns empty columns and boxes."""
        columns, ordered = process_page_layout([])
        assert columns == []
        assert ordered == []


class TestAlignmentValidator:
    """Tests for the S1∩S2 alignment algorithm."""

    def test_black_status_direct_match(self) -> None:
        """A character directly in S2 receives BLACK status."""
        from sinonom_ocr.alignment_validator import AlignmentStatus, SinoNomAlignmentValidator

        v = SinoNomAlignmentValidator()
        result = v.validate_pair("百", "trăm")
        assert result.status == AlignmentStatus.BLACK

    def test_red_status_unknown_char(self) -> None:
        """An unknown character with no S1 or S2 match receives RED status."""
        from sinonom_ocr.alignment_validator import AlignmentStatus, SinoNomAlignmentValidator

        v = SinoNomAlignmentValidator()
        result = v.validate_pair("X", "trăm")
        assert result.status == AlignmentStatus.RED

    def test_sequence_accuracy_all_black(self) -> None:
        """A perfectly matching sequence has 100% accuracy."""
        from sinonom_ocr.alignment_validator import SinoNomAlignmentValidator

        v = SinoNomAlignmentValidator()
        result = v.validate_sequence(
            ["百", "年", "身", "後", "名"],
            ["trăm", "năm", "thân", "sau", "danh"],
        )
        assert result.accuracy == pytest.approx(1.0)
