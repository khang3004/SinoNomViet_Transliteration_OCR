"""Translation registry + runner integration tests (no network)."""

from __future__ import annotations

import pytest

from pipeline import translate
from pipeline.config import Config
from pipeline.runner import _apply_translation
from pipeline.schema import Record, SourceOf


def _register_stub_translator(name: str = "stub_mt"):
    """Register a tiny keyless translator stub for runner-integration tests."""

    class _Stub:
        name = "stub_mt"
        source_tag = "stub_mt"

        def __init__(self, config=None):
            pass

        def translate(self, han, context=""):
            return f"[VI] {han}" if han.strip() else ""

        def translate_many(self, items):
            return [self.translate(h, c) for h, c in items]

    translate.register(name, _Stub)
    return name


def test_builtin_translators_registered():
    for name in ("api", "offline", "skip"):
        assert name in translate.available()


def test_skip_translator_is_noop():
    t = translate.get_translator(Config(translate_backend="skip"))
    assert t.translate("平定") == ""


def test_offline_translator_is_stub():
    t = translate.get_translator(Config(translate_backend="offline"))
    with pytest.raises(NotImplementedError):
        t.translate("平定")


def test_translation_fills_empty_meaning():
    name = _register_stub_translator()
    recs = [Record(id="x", source_doc="d", page=1, line_no=1, han="平定營", meaning="")]
    _apply_translation(recs, Config(translate_backend=name))
    assert recs[0].meaning == "[VI] 平定營"
    assert recs[0].source_of.meaning == "stub_mt"


def test_translation_does_not_overwrite_pdf_text_meaning():
    name = _register_stub_translator()
    # two_column records arrive with a high-trust pdf_text meaning already set.
    recs = [
        Record(
            id="x",
            source_doc="ChauBan",
            page=1,
            line_no=1,
            han="平定營",
            meaning="Đã có nghĩa từ PDF",
            source_of=SourceOf(han="ocr", meaning="pdf_text"),
        )
    ]
    _apply_translation(recs, Config(translate_backend=name))
    assert recs[0].meaning == "Đã có nghĩa từ PDF"
    assert recs[0].source_of.meaning == "pdf_text"  # untouched


def test_api_backend_requires_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        translate.get_translator(Config(translate_backend="api"))
