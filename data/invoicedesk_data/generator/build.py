"""Truth -> file bytes + gold record, for every medium (native, scanned, photo, handwritten)."""

from __future__ import annotations

import hashlib
import random
import textwrap
from dataclasses import dataclass
from typing import Literal

import numpy as np
from invoicedesk.preprocess import images as pimages
from invoicedesk.preprocess import pdf as ppdf

from invoicedesk_data.generator import augment
from invoicedesk_data.generator.handwriting import write_text
from invoicedesk_data.generator.render import Renderer
from invoicedesk_data.generator.truth import BoxGold, InvoiceTruth
from invoicedesk_data.gold import EmailFixture, GoldRecord

__all__ = [
    "GENERATOR_VERSION",
    "Capture",
    "GeneratedDoc",
    "Output",
    "build_document",
    "processed_page_hashes",
]

GENERATOR_VERSION = "1.0.0"
Output = Literal["auto", "pdf", "jpeg", "png", "image_pdf"]
Capture = Literal["auto", "scan", "photo"]

_MONEYISH = ("subtotal", "tax", "total", ".amount", ".unit_price", ".quantity")


@dataclass
class GeneratedDoc:
    filename: str
    mime: str
    data: bytes
    gold: GoldRecord


def processed_page_hashes(data: bytes, mime: str) -> list[str]:
    """SHA-256 of each page PNG exactly as ``invoicedesk.preprocess`` would store it."""
    out: list[str] = []
    if mime == "application/pdf":
        doc, _ = ppdf.open_sanitized(data)
        try:
            for page in doc:
                rp = ppdf.render_page(page)
                out.append(hashlib.sha256(pimages.encode_png(rp.image)).hexdigest())
        finally:
            ppdf.close_quietly(doc)
        return out
    img, _, _ = pimages.load_raster(data)
    img, _ = pimages.resize_long_edge(img)
    return [hashlib.sha256(pimages.encode_png(img)).hexdigest()]


def _rng(truth: InvoiceTruth, salt: str) -> random.Random:
    extra = truth.params.get("augment_salt", "")
    key = f"{truth.doc_id}:{salt}:{extra}".encode()
    return random.Random(int(hashlib.sha256(key).hexdigest()[:16], 16))  # noqa: S311


def build_document(  # noqa: PLR0915
    truth: InvoiceTruth,
    renderer: Renderer,
    *,
    output: Output = "auto",
    filename: str | None = None,
    split: str | None = None,
    use_augraphy: bool = False,
    email: EmailFixture | None = None,
    priors: list[str] | None = None,
    category: str | None = None,
    description: str = "",
    capture: Capture = "auto",
) -> GeneratedDoc:
    rendered = renderer.render(truth)
    rng = _rng(truth, "augment")
    img = np.array(rendered.image, dtype=np.uint8)
    boxes: dict[str, BoxGold] = dict(rendered.boxes)
    hidden = list(rendered.hidden_boxes)
    params: dict[str, object] = {}
    modified = False

    # handwriting into the transparent slots
    if rendered.hand_slots:
        hrng = _rng(truth, "hand")
        for name in sorted(rendered.hand_slots):
            slot, font_px = rendered.hand_slots[name]
            ink = write_text(
                img,
                slot.text,
                slot.box,
                hrng,
                font_px=font_px,
                illegible=name in truth.illegible_fields,
                align_right=name.endswith(_MONEYISH),
            )
            boxes[name] = BoxGold(box=ink, text=slot.text)
        modified = True

    # faint text printed onto the page's blank area below the content (then photographed)
    faint = [h for h in truth.hidden if h.style == "faint"]
    if faint:
        content_bottom = max((b.box[3] for b in boxes.values()), default=900)
        y = min(content_bottom + 70, 1360)
        for h in faint:
            size_px = int(h.font_pt * 96 / 72 * img.shape[0] / 1056)
            lines = textwrap.wrap(h.text, width=max(20, int(1050 / (0.52 * size_px))))
            fb = augment.draw_faint_text(
                img, lines, origin=(100, y), contrast=h.contrast or 0.05, size_px=size_px
            )
            hidden.extend(BoxGold(box=b, text=ln) for b, ln in zip(fb, lines, strict=True))
            y = fb[-1][3] + size_px
        modified = True

    medium = truth.medium
    if medium == "handwritten" and capture == "auto":
        capture = "photo" if rng.random() < 0.6 else "scan"
    elif medium == "photo":
        capture = "photo"
    elif medium == "scanned":
        capture = "scan"

    if medium == "native" and not modified:
        data, mime, ext = rendered.pdf, "application/pdf", "pdf"
        size = rendered.image.size
    elif capture == "scan" or (medium == "native" and modified):
        out_img, m, p = augment.scan(img, rng, use_augraphy=use_augraphy)
        params.update(p)
        size = (out_img.shape[1], out_img.shape[0])
        boxes = {
            k: b.model_copy(update={"box": augment.transform_box(b.box, m, size)})
            for k, b in boxes.items()
        }
        hidden = [
            b.model_copy(update={"box": augment.transform_box(b.box, m, size)}) for b in hidden
        ]
        kind = output
        if kind == "auto":
            r = rng.random()
            kind = "image_pdf" if r < 0.5 else ("jpeg" if r < 0.85 else "png")
        if kind == "image_pdf":
            data, mime, ext = augment.image_pdf(out_img), "application/pdf", "pdf"
        elif kind == "png":
            data, mime, ext = augment.encode_png(out_img), "image/png", "png"
        else:
            data, mime, ext = augment.encode_jpeg(out_img, rng.randint(80, 92)), "image/jpeg", "jpg"
        params["output"] = kind
    else:
        out_img, hm, p = augment.photo(img, rng)
        params.update(p)
        size = (out_img.shape[1], out_img.shape[0])
        boxes = {
            k: b.model_copy(update={"box": augment.transform_box(b.box, hm, size)})
            for k, b in boxes.items()
        }
        hidden = [
            b.model_copy(update={"box": augment.transform_box(b.box, hm, size)}) for b in hidden
        ]
        data, mime, ext = augment.encode_jpeg(out_img, rng.randint(82, 92)), "image/jpeg", "jpg"
        params["output"] = "jpeg"
    params["capture"] = capture if medium != "native" or modified else "none"
    params["size"] = list(size)

    fname = filename or f"{truth.doc_id}.{ext}"
    if not fname.endswith(f".{ext}"):
        fname = f"{fname.rsplit('.', 1)[0]}.{ext}"
    extraction = truth.to_extraction(boxes).model_dump(mode="json")
    gold = GoldRecord(
        id=truth.doc_id,
        set=truth.set_name,
        split=split,
        medium=medium,
        template=truth.template,
        filename=fname,
        mime=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        page_sha256=processed_page_hashes(data, mime),
        box_eval=medium in ("native", "scanned"),
        extraction=extraction,
        boxes=boxes,
        hidden_boxes=hidden,
        expected=truth.expected,
        truth=truth,
        augment=params,
        email=email,
        priors=list(priors or []),
        category=category,
        description=description,
    )
    return GeneratedDoc(filename=fname, mime=mime, data=data, gold=gold)
