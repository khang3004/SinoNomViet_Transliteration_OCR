"""Registry + config tests (extensibility + secret-safety)."""

from __future__ import annotations

import logging

import pytest

from pipeline import layouts, ocr
from pipeline.config import Config


def test_builtin_ocr_engines_registered():
    for name in ("paddle", "vision", "kandianguji", "mock"):
        assert name in ocr.available()


def test_kandianguji_stub_registers_but_not_implemented():
    engine = ocr.get_engine("kandianguji")
    with pytest.raises(NotImplementedError):
        engine.ocr("x.png")


def test_new_engine_added_with_single_call():
    class Tmp:
        name = "tmp_engine"

        def ocr(self, image):  # noqa: ARG002
            return []

    ocr.register("tmp_engine", Tmp)
    assert "tmp_engine" in ocr.available()
    assert ocr.get_engine("tmp_engine").ocr("x") == []


def test_layout_router_priority_order():
    order = layouts.available()
    assert order[0] == "two_column"  # PRIMARY checked first


def test_validate_fails_fast_when_api_key_missing(monkeypatch):
    monkeypatch.setenv("TRANSLATE_BACKEND", "api")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    cfg = Config()
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        cfg.validate()


def test_log_key_presence_logs_no_values(monkeypatch, caplog):
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-value")
    cfg = Config()
    with caplog.at_level(logging.INFO):
        cfg.log_key_presence()
    assert "super-secret-value" not in caplog.text
    assert "present: True" in caplog.text
