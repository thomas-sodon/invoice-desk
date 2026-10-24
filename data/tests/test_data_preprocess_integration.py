"""The generated files through the real preprocessor (RapidOCR): box gold matches OCR words,
hidden text reaches the text layer, the demo's faint text is recoverable by the CLAHE pass."""

from __future__ import annotations

import re

import pytest
from invoicedesk.preprocess import build_preprocessor
from invoicedesk.storage import InMemoryStorage
from invoicedesk_data.demo import FAINT_TEXT_INSTRUCTION, WHITE_TEXT_INSTRUCTION, demo_files
from invoicedesk_data.gold import load_record

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def pre():
    return build_preprocessor(InMemoryStorage())


@pytest.fixture(scope="module")
def processed(pre):
    """Every demo file preprocessed once: {gold_id: PreprocessedDocument}."""
    return {
        f.gold_id: pre.preprocess(f.data, mime=f.mime, document_id=f"t{f.gold_id[-3:]}")
        for f in demo_files()
    }


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def test_box_hit_rate_on_native_and_scanned_demo_files(processed) -> None:
    hits = total = 0
    for f in demo_files():
        if f.medium not in ("native", "scanned"):
            continue
        rec = load_record("demo", f.gold_id)
        doc = processed[f.gold_id]
        for name in (
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "total",
            "subtotal",
            "work_order_number",
        ):
            b = rec.boxes.get(name)
            if b is None:
                continue
            x0, y0, x1, y1 = b.box
            inside = [
                w
                for w in doc.words
                if w.page == 0
                and _iou(w.box, b.box) > 0
                and x0 - 4 <= (w.box[0] + w.box[2]) / 2 <= x1 + 4
                and y0 - 4 <= (w.box[1] + w.box[3]) / 2 <= y1 + 4
            ]
            total += 1
            if inside:
                union = (
                    min(w.box[0] for w in inside),
                    min(w.box[1] for w in inside),
                    max(w.box[2] for w in inside),
                    max(w.box[3] for w in inside),
                )
                hits += _iou(union, b.box) >= 0.5
    assert total >= 50
    assert hits / total >= 0.9, (hits, total)


def test_white_text_demo_pdf_exposes_hidden_span(processed) -> None:
    f = next(f for f in demo_files() if f.expected and "hidden_text" in f.expected.detectors)
    doc = processed[f.gold_id]
    layer = " ".join(s.text for s in doc.text_layer)
    assert _norm(WHITE_TEXT_INSTRUCTION)[:60] in _norm(layer)
    white = [s for s in doc.text_layer if s.fill_rgb == (255, 255, 255)]
    assert white


def test_faint_text_demo_photo_is_recovered_only_by_the_enhanced_pass(processed) -> None:
    f = next(f for f in demo_files() if f.expected and "faint_text" in f.expected.detectors)
    doc = processed[f.gold_id]
    normal = _norm(" ".join(w.text for w in doc.words))
    enhanced = _norm(" ".join(w.text for w in doc.enhanced_words))
    assert "000555067890" in enhanced
    assert "000555067890" not in normal
    assert "account" in _norm(FAINT_TEXT_INSTRUCTION)


def test_oracle_classifier_on_real_ocr_of_the_demo_batch(processed) -> None:
    """Clean demo files read as clean; the two screened files do not (same text a guardrail
    classifier would get: normal OCR, enhanced OCR and the PDF text layer)."""
    from invoicedesk_data.oracle import classify_text

    for f in demo_files():
        doc = processed[f.gold_id]
        text = " ".join(
            [w.text for w in doc.words]
            + [w.text for w in doc.enhanced_words]
            + [s.text for s in doc.text_layer]
        )
        v = classify_text(text)
        assert f.expected is not None
        if f.expected.detectors:
            assert v.verdict != "clean", f.filename
        else:
            assert v.verdict == "clean", (f.filename, v)
