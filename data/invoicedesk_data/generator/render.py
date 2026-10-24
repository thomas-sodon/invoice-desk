"""Render an :class:`InvoiceTruth` to PDF with Playwright and read gold boxes from the DOM.

Coordinates: templates lay out a US-letter page of 816 x 1056 CSS px. The PDF is rasterized
with the preprocessor's own ``render_page`` (long edge exactly 1,600 px, i.e. 1236 x 1600), and
DOM boxes are scaled with the same ``sx``/``sy`` it uses, so gold boxes are in the processed
coordinates the model and OCR see. Each box is then tightened to the ink inside it, which is
what OCR word boxes measure.

Determinism: Chromium stamps creation dates and a random file ID into PDFs; :func:`normalize_pdf`
re-saves through PyMuPDF with fixed metadata and no new ID, so the same truth gives the same
bytes (asserted in tests).

Playwright: ``playwright==1.63.0`` with the preinstalled browsers in ``PLAYWRIGHT_BROWSERS_PATH``
(default ``/opt/pw-browsers``). Never run ``playwright install`` here.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import numpy as np
import pymupdf
from invoicedesk.money import format_cents
from invoicedesk.preprocess.pdf import render_page
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from PIL import Image

from invoicedesk_data.generator.sampler import fmt_date
from invoicedesk_data.generator.truth import BoxGold, InvoiceTruth
from invoicedesk_data.paths import TEMPLATES_DIR

__all__ = [
    "CSS_H",
    "CSS_W",
    "RenderedDoc",
    "Renderer",
    "build_view",
    "normalize_pdf",
    "rasterize_pdf",
    "tighten_box",
]

CSS_W, CSS_H = 816, 1056
_FIXED_META = {
    "title": "Invoice",
    "author": "",
    "subject": "",
    "keywords": "",
    "creator": "InvoiceDesk synthetic generator",
    "producer": "InvoiceDesk synthetic generator",
    "creationDate": "D:20261020000000Z",
    "modDate": "D:20261020000000Z",
}

_STYLE: dict[str, tuple[str, bool]] = {
    "trades": ("us", True),
    "carbon_copy": ("us_short", False),
    "quickbooks": ("us", True),
    "word": ("long", True),
    "modern": ("dmy", True),
    "letterhead": ("abbr", True),
    "compact": ("iso", False),
    "statement": ("us_short", True),
    "letter": ("long", True),
}

_PAPERS = ("#fffbe3", "#fdeef1", "#ffffff", "#f3f8ff")

#: Vertical slack (CSS px) each template leaves for a layout offset, measured with five line
#: items, remit-to text and notes, keeping clear of the adversarial text band (y > 900).
MAX_PAD: dict[str, int] = {
    "trades": 200,
    "carbon_copy": 120,
    "quickbooks": 200,
    "word": 140,
    "modern": 200,
    "letterhead": 200,
    "compact": 200,
    "statement": 200,
    "letter": 60,
}
LAYOUT_LEVELS = 5


def layout_level(t: InvoiceTruth) -> int:
    """Per-vendor layout offset bucket (vendors customize their templates). Same-template
    invoices from different vendors therefore differ in coarse layout, which is what a
    perceptual hash sees. ``params["layout_level"]`` overrides it."""
    if "layout_level" in t.params:
        return int(t.params["layout_level"]) % LAYOUT_LEVELS
    return _stable_int(t.vendor_slug or t.vendor_name, "layout") % LAYOUT_LEVELS


def top_pad(t: InvoiceTruth) -> int:
    template = "letter" if t.doc_type == "bank_change_letter" else t.template
    slack = MAX_PAD.get(template, 100)
    return layout_level(t) * slack // (LAYOUT_LEVELS - 1) + _stable_int(t.doc_id, "top") % 6


_BOX_JS = """
() => {
  const rect = (el) => {
    let r = el.getBoundingClientRect();
    let b = [r.left, r.top, r.right, r.bottom];
    for (const c of el.querySelectorAll('*')) {
      const q = c.getBoundingClientRect();
      if (q.width > 0 && q.height > 0) {
        b = [Math.min(b[0], q.left), Math.min(b[1], q.top), Math.max(b[2], q.right),
             Math.max(b[3], q.bottom)];
      }
    }
    return b;
  };
  const gold = [...document.querySelectorAll('[data-gold]')].map((el) => ({
    name: el.dataset.gold, text: el.dataset.text, hand: el.classList.contains('hand'),
    box: rect(el), fontPx: parseFloat(getComputedStyle(el).fontSize) }));
  const adv = [...document.querySelectorAll('[data-adv]')].map((el) => ({
    idx: parseInt(el.dataset.adv), box: rect(el) }));
  return { gold, adv, scrollH: document.documentElement.scrollHeight,
           scrollW: document.documentElement.scrollWidth };
}
"""


@dataclass
class RenderedDoc:
    pdf: bytes
    image: Image.Image  # 1236 x 1600 RGB raster of page 0 (preprocessor geometry)
    boxes: dict[str, BoxGold]
    hidden_boxes: list[BoxGold] = field(default_factory=list)
    hand_slots: dict[str, tuple[BoxGold, float]] = field(default_factory=dict)  # box, font px


def _stable_int(*parts: object) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _money(cents: int | None, symbol: bool) -> str | None:
    return None if cents is None else format_cents(cents, symbol=symbol)


def build_view(t: InvoiceTruth) -> dict[str, Any]:
    """Format a truth into the template context (printed strings only)."""
    date_style, symbol = _STYLE.get(t.template, ("us", True))
    addr = t.vendor_address
    address_lines = addr.split(", ", 1) if addr else []
    hidden_html: list[str] = []
    for i, h in enumerate(t.hidden):
        top = 940 - 26 * i
        if h.style == "white":
            hidden_html.append(
                f'<div class="adv" data-adv="{i}" style="left:64px;top:{top}px;width:690px;'
                f'color:#ffffff;font-size:{h.font_pt}pt">{_esc(h.text)}</div>'
            )
        elif h.style == "tiny":
            hidden_html.append(
                f'<div class="adv" data-adv="{i}" style="left:64px;top:{top + 40}px;width:690px;'
                f'color:#2a2a2a;font-size:{h.font_pt}pt;line-height:1.1">{_esc(h.text)}</div>'
            )
    rate = t.tax_rate_bps
    tax_label = f"Sales tax ({rate / 100:.2f}%)" if rate else "Tax"
    terms_days = (t.due_date - t.invoice_date).days if t.due_date and t.invoice_date else None
    return {
        "vendor_name": t.vendor_name,
        "vendor_address": addr,
        "address_lines": address_lines,
        "phone": t.vendor_phone or "",
        "email": t.vendor_email or "",
        "tax_id_printed": f"00-000{t.vendor_tax_id_last4}" if t.vendor_tax_id_last4 else None,
        "invoice_number": t.invoice_number,
        "invoice_date": fmt_date(t.invoice_date, date_style) if t.invoice_date else None,
        "due_date": fmt_date(t.due_date, date_style) if t.due_date else None,
        "wo": t.work_order_printed,
        "unit": t.property_or_unit,
        "bill_to_name": t.bill_to_name,
        "bill_to_address": t.bill_to_address,
        "lines": [
            {
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_price": _money(ln.unit_price_cents, symbol),
                "amount": _money(ln.amount_cents, symbol),
            }
            for ln in t.lines
        ],
        "subtotal": _money(t.subtotal_cents, symbol),
        "tax": _money(t.tax_cents, symbol),
        "tax_label": tax_label,
        "total": _money(t.total_cents, symbol),
        "remit": t.remit_text,
        "notes": t.notes,
        "terms": f"Net {terms_days}" if terms_days and terms_days > 0 else "Due on receipt",
        "accent": t.accent,
        "hidden_html": hidden_html,
        "hand": t.medium == "handwritten",
        "letter_body": t.letter_body,
        "signer": t.params.get("signer", "Jordan Ellery"),
        "tagline": t.params.get("tagline", "Licensed, bonded and insured"),
        "top_pad": top_pad(t),
        "paper": _PAPERS[_stable_int(t.doc_id, "paper") % len(_PAPERS)],
    }


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_pdf(data: bytes, *, offpage: list[tuple[str, float]] | None = None) -> bytes:
    """Fixed metadata, no random file ID; optionally add off-page text (adversarial)."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        doc.set_metadata(_FIXED_META)
        for i, (text, size) in enumerate(offpage or []):
            page = doc[0]
            # Beyond the right edge of the media box: in the text layer, never on screen.
            page.insert_text(pymupdf.Point(page.rect.width + 40, 120 + 30 * i), text, fontsize=size)
        return doc.tobytes(garbage=3, deflate=True, no_new_id=True)
    finally:
        doc.close()


def rasterize_pdf(data: bytes) -> tuple[Image.Image, float, float]:
    """Page 0 at the preprocessor's geometry. Returns ``(image, sx, sy)`` in px per CSS px."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        rp = render_page(doc[0])
        img = rp.image.convert("RGB")
        return img, img.width / CSS_W, img.height / CSS_H
    finally:
        doc.close()


def tighten_box(
    rgb: np.ndarray, box: tuple[int, int, int, int], *, thresh: int = 60
) -> tuple[int, int, int, int]:
    """Shrink ``box`` to the ink of its own text lines.

    Ink is any pixel far from the box's border colour. Rows are grouped into runs of
    consecutive ink rows (looking a little above and below the box, so a neighbouring line's
    descenders or ascenders form a run that lies mostly outside); only runs mostly inside the
    DOM box are kept."""
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    bh = max(1, y1 - y0)
    pad = max(2, bh // 3)
    cx0, cx1 = max(0, x0 - 1), min(w, x1 + 1)
    cy0, cy1 = max(0, y0 - pad), min(h, y1 + pad)
    if cx1 - cx0 < 2 or cy1 - cy0 < 2:
        return box
    gray = rgb[cy0:cy1, cx0:cx1].astype(np.int32).mean(axis=2)
    inner = gray[max(0, y0 - cy0) : max(0, y0 - cy0) + bh]
    border = (
        np.concatenate([inner[0, :], inner[-1, :], inner[:, 0], inner[:, -1]])
        if inner.size
        else gray.ravel()
    )
    bg = float(np.median(border))
    ink = np.abs(gray - bg) > thresh
    rows = ink.any(axis=1)
    keep = np.zeros_like(rows)
    i = 0
    while i < len(rows):
        if not rows[i]:
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j]:
            j += 1
        top, bot = cy0 + i, cy0 + j
        inside = max(0, min(bot, y1) - max(top, y0))
        if inside >= 0.5 * (bot - top):
            keep[i:j] = True
        i = j
    sel = ink & keep[:, None]
    ys, xs = np.nonzero(sel)
    if len(xs) == 0:
        return box
    return (
        cx0 + int(xs.min()),
        cy0 + int(ys.min()),
        cx0 + int(xs.max()) + 1,
        cy0 + int(ys.max()) + 1,
    )


class Renderer:
    """Reusable Playwright Chromium session. Use as a context manager."""

    def __init__(self) -> None:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.base_css = (TEMPLATES_DIR / "_base.css").read_text(encoding="utf-8")
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    def __enter__(self) -> Renderer:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(viewport={"width": CSS_W, "height": CSS_H})
        self._page.emulate_media(media="print")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._browser = self._pw = self._page = None

    def html(self, t: InvoiceTruth) -> str:
        template = "letter" if t.doc_type == "bank_change_letter" else t.template
        tpl = self.env.get_template(f"{template}.html.j2")
        return tpl.render(doc=build_view(t), base_css=self.base_css)

    def render(self, t: InvoiceTruth) -> RenderedDoc:
        if self._page is None:
            raise RuntimeError("Renderer must be used as a context manager")
        page = self._page
        page.set_content(self.html(t), wait_until="load")
        page.evaluate("document.fonts.ready")
        info = page.evaluate(_BOX_JS)
        if info["scrollH"] > CSS_H + 1 or info["scrollW"] > CSS_W + 1:
            raise RuntimeError(f"{t.doc_id}: content overflows the page ({info})")
        raw = page.pdf(
            width="8.5in",
            height="11in",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            prefer_css_page_size=True,
        )
        offpage = [(h.text, h.font_pt) for h in t.hidden if h.style == "offpage"]
        pdf = normalize_pdf(raw, offpage=offpage)
        img, sx, sy = rasterize_pdf(pdf)
        rgb = np.asarray(img)

        def scale(b: list[float]) -> tuple[int, int, int, int]:
            return (
                int(np.floor(b[0] * sx)),
                int(np.floor(b[1] * sy)),
                int(np.ceil(b[2] * sx)),
                int(np.ceil(b[3] * sy)),
            )

        boxes: dict[str, BoxGold] = {}
        hand: dict[str, tuple[BoxGold, float]] = {}
        for g in info["gold"]:
            dom = scale(g["box"])
            if g["hand"]:
                hand[g["name"]] = (BoxGold(box=dom, text=g["text"]), float(g["fontPx"]) * sy)
                continue
            boxes[g["name"]] = BoxGold(box=tighten_box(rgb, dom), text=g["text"])
        hidden = [
            BoxGold(box=scale(a["box"]), text=t.hidden[a["idx"]].text)
            for a in sorted(info["adv"], key=lambda a: a["idx"])
        ]
        return RenderedDoc(pdf=pdf, image=img, boxes=boxes, hidden_boxes=hidden, hand_slots=hand)
