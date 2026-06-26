"""Real-PDF two_column path — coordinate-scaled hybrid extraction.

Proves the PDF code path (text-layer spans scaled to render_dpi pixel space +
Han OCR of the rendered left-column crop) pairs correctly, WITHOUT poppler or a
real PDF: ``render_page``, ``extract_spans`` and ``has_text_layer`` are
monkeypatched, and the OCR engine is a fake.

Trick that makes this a real proof of the SCALING: the text-layer spans are
provided in POINT space at HALF the mock-fixture pixel coordinates, and the page
is "rendered" at 144 dpi (scale = 2.0). So the scaled pixel spans land exactly on
the known-good mock pixel coordinates, and pairing must reproduce the same
records as the pure-mock two_column test (entry 99 → 平定營公堂官, 100 → 諭旨).
"""

from __future__ import annotations

import pipeline.page_context as pc_mod
from pipeline import layouts
from pipeline.config import Config
from pipeline.page_context import PageContext
from pipeline.pdf_text import TextSpan
from tests.fixtures.mock_two_column import PAGE_WIDTH, mock_han_ocr, mock_text_spans


def _point_spans() -> list[TextSpan]:
    # POINT-space spans = mock PIXEL spans / 2, so a 144-dpi render (scale 2.0)
    # scales them back onto the known-good mock pixel coordinates.
    return [TextSpan(s.text, s.x0 / 2, s.y0 / 2, s.x1 / 2, s.y1 / 2) for s in mock_text_spans()]


class _FakeCrop:
    def save(self, path):  # noqa: ARG002
        pass


class _FakeImage:
    width = 600
    height = 800

    def crop(self, box):  # noqa: ARG002
        return _FakeCrop()


class _FakeEngine:
    name = "fake"

    def ocr(self, image):  # noqa: ARG002
        # The Han OCR runs on the rendered left-column crop; fixture coords are
        # already in the (144-dpi) pixel space the scaled spans live in.
        return mock_han_ocr()


def _patch(monkeypatch):
    monkeypatch.setattr(pc_mod, "has_text_layer", lambda *a, **k: True)

    def fake_extract(path, idx=0, scale=1.0):  # noqa: ARG001
        return [
            TextSpan(s.text, s.x0 * scale, s.y0 * scale, s.x1 * scale, s.y1 * scale)
            for s in _point_spans()
        ]

    monkeypatch.setattr(pc_mod, "extract_spans", fake_extract)
    monkeypatch.setattr(pc_mod, "render_page", lambda path, idx, dpi: (_FakeImage(), dpi / 72.0))


def _ctx() -> PageContext:
    return PageContext(
        source_doc="ChauBan",
        page=43,
        pdf_path="fake.pdf",
        render_dpi=144,  # scale = 2.0
        page_width=PAGE_WIDTH,
        ocr_engine=_FakeEngine(),
        config=Config(translate_backend="skip"),
    )


def test_pdf_router_selects_two_column(monkeypatch):
    _patch(monkeypatch)
    assert layouts.route(_ctx()).name == "two_column"


def test_pdf_pairing_after_coordinate_scaling(monkeypatch):
    _patch(monkeypatch)
    recs = {r.entry_no: r for r in layouts.get_handler("two_column").extract(_ctx())}
    assert recs[99].han == "平定營公堂官"
    assert recs[100].han == "諭旨"
    assert "LƯU TRỮ" not in recs[99].han
    assert recs[99].entry_meta.to_tap == "67/1"
    assert recs[99].source_of.meaning == "pdf_text"
    assert recs[99].meaning.startswith("Công đường quan doanh Bình Định")
