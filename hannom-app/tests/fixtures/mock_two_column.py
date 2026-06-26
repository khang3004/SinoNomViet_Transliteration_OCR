"""MOCK fixtures for the two_column dry-run/tests (AGENTS.md §1, §11.4).

The actual synthetic data now lives in ``pipeline.demo_data`` so the web UI demo
and the tests share one source of truth. This module re-exports it under the
historical ``mock_*`` names used by the dry-run script and tests.
"""

from __future__ import annotations

from pipeline.demo_data import PAGE_WIDTH, demo_han_ocr, demo_text_spans

__all__ = ["PAGE_WIDTH", "mock_text_spans", "mock_han_ocr"]

mock_text_spans = demo_text_spans
mock_han_ocr = demo_han_ocr
