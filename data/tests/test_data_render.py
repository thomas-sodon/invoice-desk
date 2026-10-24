"""Rendering: deterministic bytes, gold boxes aligned with the rendered text."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, cast

import pymupdf
import pytest
from invoicedesk_data.generator.build import build_document
from invoicedesk_data.generator.render import Renderer, rasterize_pdf
from invoicedesk_data.generator.sampler import TruthSampler
from invoicedesk_data.generator.truth import HiddenText, InvoiceTruth
from invoicedesk_data.universe import TEMPLATES, build_universe

pytestmark = pytest.mark.slow


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


@pytest.fixture(scope="module")
def renderer():
    with Renderer() as r:
        yield r


@pytest.fixture(scope="module")
def sample_truths() -> list[InvoiceTruth]:
    u = build_universe()
    s = TruthSampler(
        u,
        seed=77,
        set_name="test",
        prefix="t",
        window=(dt.date(2026, 8, 1), dt.date(2026, 9, 20)),
        number_offset=900,
    )
    out = [s.clean("native", template=t) for t in TEMPLATES]
    out.append(s.with_defect("missing_wo", "native", template="word"))
    return out


def test_pdf_bytes_are_deterministic(renderer, sample_truths) -> None:
    for t in sample_truths[:3]:
        a = renderer.render(t).pdf
        b = renderer.render(t).pdf
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
        doc = pymupdf.open(stream=a, filetype="pdf")
        assert (doc.metadata or {})["creationDate"] == "D:20261020000000Z"
        assert doc.page_count == 1
        doc.close()


def test_gold_boxes_align_with_pdf_text(renderer, sample_truths) -> None:
    """Every gold box holds exactly the gold text, measured on the PDF's own text layer."""
    for t in sample_truths:
        r = renderer.render(t)
        img, _, _ = rasterize_pdf(r.pdf)
        doc = pymupdf.open(stream=r.pdf, filetype="pdf")
        page = doc[0]
        kx, ky = img.width / page.rect.width, img.height / page.rect.height
        words = [(w[0] * kx, w[1] * ky, w[2] * kx, w[3] * ky, w[4]) for w in page.get_text("words")]
        doc.close()
        assert r.boxes, t.template
        for name, b in r.boxes.items():
            x0, y0, x1, y1 = b.box
            inside = [
                w
                for w in words
                if x0 - 3 <= (w[0] + w[2]) / 2 <= x1 + 3 and y0 - 3 <= (w[1] + w[3]) / 2 <= y1 + 3
            ]
            got = _norm("".join(w[4] for w in inside))
            assert got == _norm(b.text), (t.template, name, got, b.text)
            union = (
                min(w[0] for w in inside),
                min(w[1] for w in inside),
                max(w[2] for w in inside),
                max(w[3] for w in inside),
            )
            # Gold boxes are ink-tight; PDF word boxes span the full line height. So: the gold
            # box lies inside the words' box, and covers the same horizontal extent.
            assert union[0] - 2 <= x0 and union[1] - 2 <= y0, (t.template, name, b.box, union)
            assert x1 <= union[2] + 2 and y1 <= union[3] + 2, (t.template, name, b.box, union)
            slack = max(4.0, 0.08 * (union[2] - union[0]))  # glyph side bearings
            assert x0 - union[0] <= slack and union[2] - x1 <= slack, (name, b.box, union)
            assert (y1 - y0) >= 0.4 * (union[3] - union[1]), (name, b.box, union)


def test_every_template_marks_the_required_fields(renderer, sample_truths) -> None:
    required = {
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "total",
        "subtotal",
        "tax",
        "line_items.0.amount",
        "line_items.0.description",
    }
    for t in sample_truths[: len(TEMPLATES)]:
        r = renderer.render(t)
        assert required <= set(r.boxes), (t.template, required - set(r.boxes))
        if t.work_order_number:
            assert "work_order_number" in r.boxes


def test_documents_are_deterministic_per_medium(renderer, sample_truths) -> None:
    t = sample_truths[0]
    for medium in ("native", "scanned", "photo", "handwritten"):
        tm = t.model_copy(update={"medium": medium})
        a = build_document(tm, renderer)
        b = build_document(tm, renderer)
        assert a.gold.sha256 == b.gold.sha256, medium
        assert a.gold.boxes == b.gold.boxes
        assert a.gold.page_sha256 == b.gold.page_sha256 and len(a.gold.page_sha256) == 1


def test_handwritten_boxes_cover_the_ink(renderer, sample_truths) -> None:
    import numpy as np
    from PIL import Image

    t = sample_truths[1].model_copy(update={"medium": "handwritten", "template": "carbon_copy"})
    r = renderer.render(t)
    assert r.hand_slots, "carbon copy in handwriting mode must have hand slots"
    d = build_document(t, renderer, capture="scan", output="png")
    img = np.asarray(Image.open(__import__("io").BytesIO(d.data)).convert("L"))
    for name in r.hand_slots:
        x0, y0, x1, y1 = d.gold.boxes[name].box
        crop = img[y0:y1, x0:x1]
        assert crop.size and crop.min() < 150, name  # ink inside the gold box


def test_hidden_text_kinds_land_in_the_pdf_text_layer(renderer, sample_truths) -> None:
    t = sample_truths[2].model_copy(
        update={
            "hidden": [
                HiddenText(text="white instruction alpha", style="white", font_pt=9),
                HiddenText(text="tiny instruction bravo", style="tiny", font_pt=2),
                HiddenText(text="offpage instruction charlie", style="offpage", font_pt=10),
            ]
        }
    )
    r = renderer.render(t)
    doc = pymupdf.open(stream=r.pdf, filetype="pdf")
    page = doc[0]
    info = cast(dict[str, Any], page.get_text("dict"))
    spans = [s for b in info["blocks"] for ln in b.get("lines", []) for s in ln["spans"]]
    by_text = {s["text"].strip(): s for s in spans}
    white = next(s for k, s in by_text.items() if "alpha" in k)
    assert white["color"] == 0xFFFFFF
    tiny = next(s for k, s in by_text.items() if "bravo" in k)
    assert tiny["size"] < 4
    raw = cast(dict[str, Any], page.get_text("rawdict", clip=pymupdf.Rect(-1e4, -1e4, 1e4, 1e4)))
    off = [
        s
        for b in raw["blocks"]
        for ln in b.get("lines", [])
        for s in ln["spans"]
        if "".join(c["c"] for c in s["chars"]).startswith("offpage")
    ]
    assert off and off[0]["bbox"][0] > page.rect.width
    doc.close()
