"""_spatial.py — vendored copy of the existing spatial layout engine.

This is a FAITHFUL, behaviour-identical copy of
``src/sinonom_ocr/spatial_layout_engine.py`` (the existing han_only / vertical
SinoNom column logic), vendored here so ``hannom-app/`` stays self-contained
(AGENTS.md §0: "all work lives under hannom-app/") and so a future container does
not need the outer ``src/`` package on its path.

The ``han_only`` and ``three_block`` handlers are thin wrappers over
``process_page_layout`` below — they do NOT change its output. The regression
dry-run (scripts/dryrun_three_block.py) imports the ORIGINAL engine from
``src/`` and asserts the handler produces identical column text, proving the
port is faithful (AGENTS.md §3.2 porting note, §11.5).

Only dependency: numpy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("hannom.layouts.spatial")


@dataclass
class BoundingBox:
    """Normalised axis-aligned bounding box derived from a 4-corner polygon."""

    raw_polygon: list[tuple[float, float]]
    text: str = ""
    confidence: float = 1.0
    column_id: int = -1
    reading_idx: int = -1

    x_min: float = field(init=False)
    y_min: float = field(init=False)
    x_max: float = field(init=False)
    y_max: float = field(init=False)
    cx: float = field(init=False)
    cy: float = field(init=False)
    width: float = field(init=False)
    height: float = field(init=False)

    def __post_init__(self) -> None:
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
        polygon = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return cls(raw_polygon=polygon, text=text, confidence=confidence)


@dataclass
class Column:
    """A single vertical text column in a SinoNom manuscript page."""

    column_id: int
    boxes: list[BoundingBox] = field(default_factory=list)
    x_center: float = 0.0
    x_span: tuple[float, float] = (0.0, 0.0)

    def sort_top_to_bottom(self) -> None:
        self.boxes.sort(key=lambda b: b.cy)

    def full_text(self, separator: str = "") -> str:
        return separator.join(b.text for b in self.boxes)


class AdaptiveHorizontalThresholdClusterer:
    """Clusters bounding boxes into vertical columns using an adaptive threshold."""

    def __init__(
        self,
        alpha: float = 0.5,
        min_boxes: int = 1,
        merge_adjacent: bool = True,
    ) -> None:
        self._alpha = alpha
        self._min_boxes = min_boxes
        self._merge_adjacent = merge_adjacent

    def cluster(self, boxes: list[BoundingBox]) -> list[Column]:
        if not boxes:
            raise ValueError("Cannot cluster an empty list of bounding boxes.")

        widths = [b.width for b in boxes if b.width > 0]
        median_width = float(np.median(widths)) if widths else 40.0
        threshold = self._alpha * median_width

        sorted_boxes = sorted(boxes, key=lambda b: b.cx, reverse=True)

        columns: list[Column] = []
        for box in sorted_boxes:
            assigned = False
            for col in columns:
                if abs(box.cx - col.x_center) <= threshold:
                    col.boxes.append(box)
                    col.x_center = float(np.mean([b.cx for b in col.boxes]))
                    assigned = True
                    break
            if not assigned:
                new_col = Column(
                    column_id=len(columns),
                    boxes=[box],
                    x_center=box.cx,
                    x_span=(box.x_min, box.x_max),
                )
                columns.append(new_col)

        if self._merge_adjacent:
            columns = self._merge_close_columns(columns, threshold * 2)

        columns = [c for c in columns if len(c.boxes) >= self._min_boxes]

        columns.sort(key=lambda c: c.x_center, reverse=True)
        for idx, col in enumerate(columns):
            col.column_id = idx
            all_x = [b.x_min for b in col.boxes] + [b.x_max for b in col.boxes]
            col.x_span = (min(all_x), max(all_x))
            col.sort_top_to_bottom()

        return columns

    @staticmethod
    def _merge_close_columns(
        columns: list[Column],
        merge_threshold: float,
    ) -> list[Column]:
        if len(columns) <= 1:
            return columns

        sorted_cols = sorted(columns, key=lambda c: c.x_center, reverse=True)
        merged: list[Column] = [sorted_cols[0]]

        for col in sorted_cols[1:]:
            prev = merged[-1]
            if abs(col.x_center - prev.x_center) <= merge_threshold:
                prev.boxes.extend(col.boxes)
                prev.x_center = float(np.mean([b.cx for b in prev.boxes]))
            else:
                merged.append(col)

        return merged


class SinoNomReadingOrderSorter:
    """Assigns global reading-order indices across all columns (RTL, top-bottom)."""

    def __init__(self, columns: list[Column]) -> None:
        self._columns = columns

    def assign(self) -> list[BoundingBox]:
        ordered_boxes: list[BoundingBox] = []
        global_idx = 0
        for col in self._columns:
            for box in col.boxes:
                box.column_id = col.column_id
                box.reading_idx = global_idx
                ordered_boxes.append(box)
                global_idx += 1
        return ordered_boxes


def process_page_layout(
    raw_boxes: list[BoundingBox],
    alpha: float = 0.5,
    min_boxes_per_column: int = 1,
    merge_adjacent: bool = True,
) -> tuple[list[Column], list[BoundingBox]]:
    """Run the full spatial layout pipeline on one page (vendored, unchanged)."""
    if not raw_boxes:
        return [], []

    clusterer = AdaptiveHorizontalThresholdClusterer(
        alpha=alpha,
        min_boxes=min_boxes_per_column,
        merge_adjacent=merge_adjacent,
    )
    columns = clusterer.cluster(raw_boxes)
    sorter = SinoNomReadingOrderSorter(columns)
    ordered_boxes = sorter.assign()
    return columns, ordered_boxes


def detections_to_boxes(detections: list[dict]) -> list[BoundingBox]:
    """Convert common OCR detections ({text,bbox,conf}) to BoundingBox list."""
    boxes: list[BoundingBox] = []
    for det in detections:
        x0, y0, x1, y1 = det["bbox"]
        boxes.append(
            BoundingBox.from_xyxy(
                x0, y0, x1, y1, text=det.get("text", ""), confidence=det.get("conf", 1.0)
            )
        )
    return boxes
