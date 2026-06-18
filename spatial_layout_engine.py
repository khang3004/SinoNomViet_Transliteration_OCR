"""
spatial_layout_engine.py
========================
Geometry processing module for classical SinoNom vertical text layout.

Classical Chinese/Nom manuscripts are written in vertical columns read
from RIGHT-to-LEFT. Each column is read from TOP-to-BOTTOM. This module:

  1. Accepts raw OCR bounding boxes (in any order).
  2. Applies an Adaptive Horizontal Threshold (AHT) algorithm to cluster
     spatially-proximate boxes into coherent vertical columns.
  3. Sorts columns strictly Right-to-Left, and tokens within each column
     strictly Top-to-Bottom.
  4. Computes derived geometric properties (centroid, reading index, etc.)

The bounding-box format follows the standard 4-corner polygon notation
used by PaddleOCR, Google Vision, and most commercial OCR APIs:
  [(x_top_left, y_top_left), (x_top_right, y_top_right),
   (x_bot_right, y_bot_right), (x_bot_left,  y_bot_left)]

Reference (Prof. Dien, HCMUS):
  SinoNom_OCR_TransliterationAlignment.pdf — Section I, Step 1.

Author: NLP Pipeline — HCMUS NaturalLanguageProcessing
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger("spatial_layout_engine")


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class BoundingBox:
    """Normalised axis-aligned bounding box derived from a 4-corner polygon.

    All coordinate values are in pixels relative to the page origin (top-left).

    Attributes:
        raw_polygon:  The original 4-point polygon as returned by OCR.
        x_min:        Left edge of the axis-aligned bounding rectangle.
        y_min:        Top edge of the axis-aligned bounding rectangle.
        x_max:        Right edge of the axis-aligned bounding rectangle.
        y_max:        Bottom edge of the axis-aligned bounding rectangle.
        text:         OCR-recognised character or text fragment inside the box.
        confidence:   OCR confidence score in [0.0, 1.0].
        column_id:    Assigned column index (0-based, right-to-left order).
        reading_idx:  Final global reading order index (0-based).
    """

    raw_polygon: list[tuple[float, float]]
    text: str = ""
    confidence: float = 1.0
    column_id: int = -1
    reading_idx: int = -1

    # Derived properties — computed in __post_init__
    x_min: float = field(init=False)
    y_min: float = field(init=False)
    x_max: float = field(init=False)
    y_max: float = field(init=False)
    cx: float = field(init=False)   # Centroid x
    cy: float = field(init=False)   # Centroid y
    width: float = field(init=False)
    height: float = field(init=False)

    def __post_init__(self) -> None:
        """Compute axis-aligned bounds and centroid from the raw polygon."""
        xs = [pt[0] for pt in self.raw_polygon]
        ys = [pt[1] for pt in self.raw_polygon]
        self.x_min = float(min(xs))
        self.y_min = float(min(ys))
        self.x_max = float(max(xs))
        self.y_max = float(max(ys))
        self.cx = (self.x_min + self.x_max) / 2.0
        self.cy = (self.y_min + self.y_max) / 2.0
        self.width = self.x_max - self.x_min
        self.height = self.y_max - self.y_min

    # ------------------------------------------------------------------
    @classmethod
    def from_xyxy(
        cls,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        text: str = "",
        confidence: float = 1.0,
    ) -> "BoundingBox":
        """Convenience constructor from (x1, y1, x2, y2) axis-aligned format.

        Args:
            x1:         Left edge.
            y1:         Top edge.
            x2:         Right edge.
            y2:         Bottom edge.
            text:       OCR text content.
            confidence: OCR confidence score.

        Returns:
            A new :class:`BoundingBox` instance.
        """
        polygon = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return cls(raw_polygon=polygon, text=text, confidence=confidence)

    # ------------------------------------------------------------------
    @classmethod
    def from_paddleocr(
        cls,
        ocr_result: tuple,
    ) -> "BoundingBox":
        """Parse a single PaddleOCR result entry.

        PaddleOCR returns entries in the format:
          ``([[x0,y0],[x1,y1],[x2,y2],[x3,y3]], (text, confidence))``

        Args:
            ocr_result: Single tuple from PaddleOCR's ``ocr.ocr()`` output.

        Returns:
            A new :class:`BoundingBox` instance.
        """
        raw_poly, (text, conf) = ocr_result
        polygon = [(float(pt[0]), float(pt[1])) for pt in raw_poly]
        return cls(raw_polygon=polygon, text=text, confidence=float(conf))

    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"BBox(col={self.column_id}, idx={self.reading_idx}, "
            f"text={self.text!r:.20s}, "
            f"x=[{self.x_min:.0f},{self.x_max:.0f}], "
            f"y=[{self.y_min:.0f},{self.y_max:.0f}])"
        )


@dataclass
class Column:
    """A single vertical text column in a SinoNom manuscript page.

    Attributes:
        column_id:    0-based column index in right-to-left reading order.
        boxes:        Ordered list of :class:`BoundingBox` objects (top→bottom).
        x_center:     Mean horizontal centroid of all boxes in this column.
        x_span:       (x_min, x_max) extents of the column band.
    """

    column_id: int
    boxes: list[BoundingBox] = field(default_factory=list)
    x_center: float = 0.0
    x_span: tuple[float, float] = (0.0, 0.0)

    def sort_top_to_bottom(self) -> None:
        """Sort contained boxes in ascending y-centroid order (top to bottom)."""
        self.boxes.sort(key=lambda b: b.cy)

    def full_text(self, separator: str = "") -> str:
        """Return the concatenated text of all boxes in reading order.

        Args:
            separator: String inserted between consecutive box texts.

        Returns:
            Full column text string.
        """
        return separator.join(b.text for b in self.boxes)


# ---------------------------------------------------------------------------
# Adaptive Horizontal Threshold algorithm
# ---------------------------------------------------------------------------

class AdaptiveHorizontalThresholdClusterer:
    """Clusters bounding boxes into vertical columns using an adaptive threshold.

    The Adaptive Horizontal Threshold (AHT) algorithm:

    1. Projects all box centroids onto the X-axis.
    2. Estimates the typical character width ``w̄`` as the median box width.
    3. Computes the adaptive threshold ``T = α × w̄`` where ``α`` is a
       configurable multiplier (default: 0.5 — half a character width).
    4. Sorts boxes by x-centroid descending (right to left).
    5. Greedily assigns each box to an existing column whose x-center is
       within ``T`` pixels, or creates a new column otherwise.

    This approach handles slight mis-alignments and skewed columns that arise
    from scanning and perspective distortion.

    Args:
        alpha:          Threshold multiplier relative to median box width.
        min_boxes:      Minimum boxes required to retain a column.
        merge_adjacent: Whether to attempt merging nearly-identical columns
                        after initial clustering.
    """

    def __init__(
        self,
        alpha: float = 0.5,
        min_boxes: int = 1,
        merge_adjacent: bool = True,
    ) -> None:
        self._alpha = alpha
        self._min_boxes = min_boxes
        self._merge_adjacent = merge_adjacent

    # ------------------------------------------------------------------
    def cluster(self, boxes: list[BoundingBox]) -> list[Column]:
        """Cluster a flat list of bounding boxes into ordered columns.

        Args:
            boxes: List of :class:`BoundingBox` objects in any order.

        Returns:
            A list of :class:`Column` objects, sorted right-to-left
            (column 0 is the rightmost column).

        Raises:
            ValueError: If ``boxes`` is empty.
        """
        if not boxes:
            raise ValueError("Cannot cluster an empty list of bounding boxes.")

        # Estimate adaptive threshold from median box width
        widths = [b.width for b in boxes if b.width > 0]
        median_width = float(np.median(widths)) if widths else 40.0
        threshold = self._alpha * median_width

        logger.debug(
            "AHT: median_width=%.1fpx, alpha=%.2f, threshold=%.1fpx",
            median_width,
            self._alpha,
            threshold,
        )

        # Sort boxes right-to-left by centroid x (descending)
        sorted_boxes = sorted(boxes, key=lambda b: b.cx, reverse=True)

        # Greedy column assignment
        columns: list[Column] = []
        for box in sorted_boxes:
            assigned = False
            for col in columns:
                if abs(box.cx - col.x_center) <= threshold:
                    col.boxes.append(box)
                    # Update running x-center
                    col.x_center = float(
                        np.mean([b.cx for b in col.boxes])
                    )
                    assigned = True
                    break
            if not assigned:
                # New column — starts with this box
                new_col = Column(
                    column_id=len(columns),
                    boxes=[box],
                    x_center=box.cx,
                    x_span=(box.x_min, box.x_max),
                )
                columns.append(new_col)

        # Optionally merge adjacent narrow columns (handles split characters)
        if self._merge_adjacent:
            columns = self._merge_close_columns(columns, threshold * 2)

        # Filter empty/sparse columns
        columns = [c for c in columns if len(c.boxes) >= self._min_boxes]

        # Sort columns right-to-left by x_center descending, assign final IDs
        columns.sort(key=lambda c: c.x_center, reverse=True)
        for idx, col in enumerate(columns):
            col.column_id = idx
            # Update x_span after potential merges
            all_x = [b.x_min for b in col.boxes] + [b.x_max for b in col.boxes]
            col.x_span = (min(all_x), max(all_x))
            # Sort boxes top to bottom within column
            col.sort_top_to_bottom()

        logger.info("AHT clustering: %d boxes → %d columns.", len(boxes), len(columns))
        return columns

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_close_columns(
        columns: list[Column],
        merge_threshold: float,
    ) -> list[Column]:
        """Merge columns whose x-centers are within ``merge_threshold`` pixels.

        This handles cases where OCR detects a single physical column as
        two closely-spaced narrow clusters.

        Args:
            columns:         Input list of columns.
            merge_threshold: Maximum x-center distance to trigger merging.

        Returns:
            Merged column list (may be shorter than input).
        """
        if len(columns) <= 1:
            return columns

        # Sort by x-center descending for merge-window scan
        sorted_cols = sorted(columns, key=lambda c: c.x_center, reverse=True)
        merged: list[Column] = [sorted_cols[0]]

        for col in sorted_cols[1:]:
            prev = merged[-1]
            if abs(col.x_center - prev.x_center) <= merge_threshold:
                # Absorb into previous
                prev.boxes.extend(col.boxes)
                prev.x_center = float(np.mean([b.cx for b in prev.boxes]))
                logger.debug(
                    "Merged column at x=%.1f into column at x=%.1f",
                    col.x_center,
                    prev.x_center,
                )
            else:
                merged.append(col)

        return merged


# ---------------------------------------------------------------------------
# Reading-order finaliser
# ---------------------------------------------------------------------------

class SinoNomReadingOrderSorter:
    """Assigns global reading-order indices to all boxes across all columns.

    Reading convention for classical SinoNom:
      - Column 0 (rightmost) is read first.
      - Within each column, boxes are read top-to-bottom.

    This class enumerates boxes in that order and writes
    :attr:`BoundingBox.reading_idx` for each box.

    Args:
        columns: Pre-clustered and sorted :class:`Column` list.
    """

    def __init__(self, columns: list[Column]) -> None:
        self._columns = columns

    # ------------------------------------------------------------------
    def assign(self) -> list[BoundingBox]:
        """Traverse all columns right-to-left, top-to-bottom and assign indices.

        Returns:
            A flat list of all :class:`BoundingBox` objects in final
            reading order, with ``column_id`` and ``reading_idx`` set.
        """
        ordered_boxes: list[BoundingBox] = []
        global_idx = 0

        for col in self._columns:  # Already sorted right-to-left
            for box in col.boxes:  # Already sorted top-to-bottom
                box.column_id = col.column_id
                box.reading_idx = global_idx
                ordered_boxes.append(box)
                global_idx += 1

        logger.debug(
            "Assigned reading indices to %d boxes across %d columns.",
            global_idx,
            len(self._columns),
        )
        return ordered_boxes


# ---------------------------------------------------------------------------
# Public pipeline function
# ---------------------------------------------------------------------------

def process_page_layout(
    raw_boxes: list[BoundingBox],
    alpha: float = 0.5,
    min_boxes_per_column: int = 1,
    merge_adjacent: bool = True,
) -> tuple[list[Column], list[BoundingBox]]:
    """Run the full spatial layout processing pipeline on one page.

    Pipeline:
      1. Cluster boxes into columns via AHT.
      2. Sort columns right-to-left, boxes top-to-bottom.
      3. Assign global reading-order indices.

    Args:
        raw_boxes:            Input bounding boxes in any order.
        alpha:                AHT threshold multiplier (0.3–0.8 typical).
        min_boxes_per_column: Minimum boxes to keep a column (filters noise).
        merge_adjacent:       Whether to merge closely-spaced column pairs.

    Returns:
        A tuple ``(columns, ordered_boxes)`` where:
        - ``columns``:      :class:`Column` list, right-to-left order.
        - ``ordered_boxes``: Flat list in final reading order.

    Example:
        >>> boxes = [BoundingBox.from_xyxy(341, 8, 379, 149, text="百"),
        ...          BoundingBox.from_xyxy(261, 9, 297, 251, text="年"),
        ...          BoundingBox.from_xyxy(181, 6, 219, 252, text="身"),
        ...          BoundingBox.from_xyxy(102, 9, 137, 250, text="後"),
        ...          BoundingBox.from_xyxy( 20, 9,  55, 250, text="名")]
        >>> columns, ordered = process_page_layout(boxes)
        >>> for b in ordered:
        ...     print(b.reading_idx, b.column_id, b.text)
    """
    if not raw_boxes:
        logger.warning("process_page_layout called with empty box list.")
        return [], []

    # Step 1: Cluster
    clusterer = AdaptiveHorizontalThresholdClusterer(
        alpha=alpha,
        min_boxes=min_boxes_per_column,
        merge_adjacent=merge_adjacent,
    )
    columns = clusterer.cluster(raw_boxes)

    # Step 2 & 3: Assign reading order
    sorter = SinoNomReadingOrderSorter(columns)
    ordered_boxes = sorter.assign()

    return columns, ordered_boxes


# ---------------------------------------------------------------------------
# Mock data helper (for testing / notebook demos)
# ---------------------------------------------------------------------------

def create_mock_ocr_response() -> list[BoundingBox]:
    """Create a mock OCR bounding-box response mirroring the example from
    Prof. Dien's SinoNom_OCR_TransliterationAlignment.pdf (Section I.c).

    The example page has 5 vertical boxes (columns), each containing one
    Sino-Nom character. Coordinates are in pixels.

    Returns:
        A list of five :class:`BoundingBox` objects.

    Note:
        In real pipeline usage, this mock is replaced by actual OCR output
        from PaddleOCR, Google Vision, or a HCMUS CLC endpoint.
    """
    mock_data: list[tuple[tuple[int, int, int, int], str, float]] = [
        # (x1, y1, x2, y2,  text,  confidence)
        (341, 8,  379, 149, "百",  0.96),  # Column 1 (rightmost)
        (261, 9,  297, 251, "年",  0.93),  # Column 2
        (181, 6,  219, 252, "身",  0.91),  # Column 3
        (102, 9,  137, 250, "後",  0.89),  # Column 4
        (20,  9,  55,  250, "名",  0.94),  # Column 5 (leftmost)
    ]

    boxes: list[BoundingBox] = []
    for x1, y1, x2, y2, text, conf in mock_data:
        boxes.append(BoundingBox.from_xyxy(x1, y1, x2, y2, text=text, confidence=conf))

    return boxes


def create_mock_multi_char_response() -> list[BoundingBox]:
    """Create a richer mock OCR response simulating a page with multiple
    characters per column — more representative of real manuscript pages.

    Returns:
        A list of :class:`BoundingBox` objects across 3 simulated columns.
    """
    # Column A (rightmost, x ≈ 360): 4 chars top→bottom
    col_a = [
        (340, 10,  380,  60,  "帝", 0.97),
        (340, 65,  380, 115,  "王", 0.95),
        (340, 120, 380, 170,  "臨", 0.92),
        (340, 175, 380, 225,  "朝", 0.90),
    ]
    # Column B (middle, x ≈ 260): 3 chars
    col_b = [
        (245, 15,  285,  65,  "聖", 0.96),
        (245, 70,  285, 120,  "德", 0.93),
        (245, 125, 285, 175,  "日", 0.88),
    ]
    # Column C (leftmost, x ≈ 160): 4 chars
    col_c = [
        (150, 10,  190,  60,  "新", 0.94),
        (150, 65,  190, 115,  "月", 0.92),
        (150, 120, 190, 170,  "異", 0.89),
        (150, 175, 190, 225,  "盛", 0.91),
    ]

    boxes: list[BoundingBox] = []
    for col in [col_a, col_b, col_c]:
        for x1, y1, x2, y2, text, conf in col:
            boxes.append(BoundingBox.from_xyxy(x1, y1, x2, y2, text=text, confidence=conf))

    return boxes


# ---------------------------------------------------------------------------
# Demonstration / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    print("=" * 60)
    print("Example 1: Prof. Dien's canonical 5-column example")
    print("=" * 60)
    boxes = create_mock_ocr_response()
    columns, ordered = process_page_layout(boxes)
    for b in ordered:
        print(
            f"  [ReadIdx={b.reading_idx}] Col={b.column_id}  "
            f"text={b.text!r}  "
            f"cx={b.cx:.0f}  cy={b.cy:.0f}"
        )

    print()
    print("=" * 60)
    print("Example 2: Multi-character column demo")
    print("=" * 60)
    boxes2 = create_mock_multi_char_response()
    columns2, ordered2 = process_page_layout(boxes2)
    for col in columns2:
        print(f"  Column {col.column_id} (x̄={col.x_center:.0f}): {col.full_text()!r}")
    print("\n  Full reading order:", " → ".join(b.text for b in ordered2))
