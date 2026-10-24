"""Regression tests from the adversarial review of the preprocessor (M3).

* Hidden text-layer words (white, 2 pt) sitting on visible words must never replace the OCR
  word in ``words`` (that would let hidden text overwrite what the extractor reads), and a
  hidden-only text layer must not make a PDF "native".
* Sanitizing must catch JavaScript in a catalog-inline name tree, inline launch actions and
  outline ``/A`` actions, and must fail closed on anything it could not strip.
* A text layer large enough to exhaust CPU is rejected, never truncated.
"""

from __future__ import annotations

import pymupdf
import pytest
from invoicedesk.contracts.preprocess import RejectedInput
from invoicedesk.preprocess import DefaultPreprocessor
from invoicedesk.preprocess import pdf as pdf_mod
from invoicedesk.preprocess.pdf import open_sanitized
from invoicedesk.preprocess.pipeline import delta_e76


def _pre(storage, ocr) -> DefaultPreprocessor:
    return DefaultPreprocessor(storage, ocr=ocr)


# ------------------------------------------------------------------ native merge


def test_hidden_substring_words_do_not_replace_visible_ocr_words(storage, fx):
    def hidden(_doc, page):
        page.insert_text((72, 400), "Lab and mat", fontsize=12, color=(1, 1, 1))  # white
        page.insert_text((72, 300), "1", fontsize=2)  # 2 pt, inside the amount below

    data = fx.make_pdf([], extra=hidden)
    visible = [
        ("Labor", (145, 785, 225, 812)),
        ("and", (233, 785, 262, 812)),
        ("materials", (270, 785, 380, 812)),
        ("1,240.00", (140, 590, 260, 612)),
    ]
    out = _pre(storage, fx.FakeOcr(words={0: visible})).preprocess(
        data, mime="application/pdf", document_id="h"
    )
    assert [(w.text, w.origin) for w in out.words] == [
        ("Labor", "ocr"),
        ("and", "ocr"),
        ("materials", "ocr"),
        ("1,240.00", "ocr"),
    ]
    # the text layer is hidden-only, so it is not a usable (native) text layer
    assert not out.is_native_pdf
    assert {s.text for s in out.text_layer} == {"Lab", "and", "mat", "1"}


def test_merged_ocr_word_is_matched_by_its_text_layer_words(storage, fx):
    data = fx.make_pdf([((72, 150), "Maple Row Property Management", 13)])
    known = dict(fx.expected_words(data))
    m, r = known["Maple"], known["Row"]
    merged = (m[0], m[1], r[2], r[3])  # OCR missed the space: "MapleRow"
    ocr = fx.FakeOcr(
        words={
            0: [
                ("MapleRow", merged),
                ("Property", known["Property"]),
                ("Management", known["Management"]),
            ]
        }
    )
    out = _pre(storage, ocr).preprocess(data, mime="application/pdf", document_id="m")
    assert out.is_native_pdf
    assert [(w.text, w.origin) for w in out.words] == [
        ("Maple", "pdf_text"),
        ("Row", "pdf_text"),
        ("Property", "pdf_text"),
        ("Management", "pdf_text"),
    ]


def test_light_text_on_dark_band_still_corroborates(storage, fx):
    def band(_doc, page):
        page.draw_rect(pymupdf.Rect(60, 130, 500, 160), color=None, fill=(0, 0, 0))
        page.insert_text((72, 150), "Maple Row Property Management", fontsize=13, color=(1, 1, 1))

    data = fx.make_pdf([], extra=band)
    known = fx.expected_words(data)
    out = _pre(storage, fx.FakeOcr(words={0: known})).preprocess(
        data, mime="application/pdf", document_id="b"
    )
    assert out.is_native_pdf
    assert all(w.origin == "pdf_text" for w in out.words)


def test_delta_e76():
    assert delta_e76((255, 255, 255), (255, 255, 255)) == pytest.approx(0.0)
    assert delta_e76((0, 0, 0), (255, 255, 255)) == pytest.approx(100.0, abs=0.1)
    assert delta_e76((250, 250, 250), (255, 255, 255)) < 5  # near-white on white is hidden


# ------------------------------------------------------------------ sanitize


def _base() -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "INVOICE INV-2231", fontsize=18)
    return doc


def test_inline_javascript_name_tree_and_launch_removed():
    doc = _base()
    cat = doc.pdf_catalog()
    doc.xref_set_key(
        cat, "Names", "<< /JavaScript << /Names [(a) << /S /JavaScript /JS (app.alert(1)) >>] >> >>"
    )
    doc.xref_set_key(doc[0].xref, "AA", "<< /O << /S /Launch /F (calc.exe) >> >>")
    clean, report = open_sanitized(doc.tobytes())
    assert report.removed_javascript
    out = clean.tobytes(garbage=4)
    assert b"/JS" not in out and b"calc.exe" not in out and b"/Launch" not in out


def test_outline_javascript_action_removed():
    doc = _base()
    doc.set_toc([[1, "Top", 1]])
    for x in range(1, doc.xref_length()):
        if "Title" in doc.xref_get_keys(x):
            doc.xref_set_key(x, "A", "<< /S /JavaScript /JS (app.alert(2)) >>")
    clean, report = open_sanitized(doc.tobytes())
    assert report.removed_javascript
    assert b"/JS" not in clean.tobytes(garbage=4)


def test_unreachable_active_content_fails_closed():
    doc = _base()
    doc.xref_set_key(doc.pdf_catalog(), "Foo", "<< /Bar << /S /JavaScript /JS (x) >> >>")
    with pytest.raises(RejectedInput, match="active content"):
        open_sanitized(doc.tobytes())


def test_font_named_like_js_is_not_active_content():
    assert not pdf_mod._ACTIVE_RE.search("<</BaseFont/JSans/Type/Font>>")
    assert not pdf_mod._ACTIVE_RE.search("<</Names<</JavaScript null>>>>")
    assert pdf_mod._ACTIVE_RE.search("<</S/JavaScript/JS(x)>>")


# ------------------------------------------------------------------ resource limits


def test_huge_text_layer_rejected_not_truncated(storage, fx, monkeypatch):
    monkeypatch.setattr(pdf_mod, "MAX_TEXT_LAYER_WORDS_PER_PAGE", 5)
    data = fx.make_pdf()  # 13 words on the page
    with pytest.raises(RejectedInput, match="large amount of text"):
        _pre(storage, fx.FakeOcr()).preprocess(data, mime="application/pdf", document_id="big")
