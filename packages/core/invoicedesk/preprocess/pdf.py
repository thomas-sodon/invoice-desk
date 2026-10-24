"""PDF handling with PyMuPDF: open, sanitize, render at 1,600 px, extract the styled text layer.

Sanitizing removes active and hidden-payload content *before* rendering and before any text
reaches a model (DESIGN §5.5 "Pre-LLM" row):

* JavaScript (``/JS``, ``/JavaScript`` name trees), ``/OpenAction`` and ``/AA`` trigger actions;
* embedded files (the ``/EmbeddedFiles`` name tree);
* AcroForm / XFA forms and their widgets;
* every annotation, including links and file attachments.

Hidden *page content* text is deliberately kept: the hidden-text detector needs it, and the
text layer is reported in :class:`TextLayerSpan` for that purpose.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pymupdf
from PIL import Image

from invoicedesk.contracts.preprocess import (
    MAX_PAGES,
    TARGET_LONG_EDGE,
    Box,
    RejectedInput,
    SanitizeReport,
    TextLayerSpan,
)
from invoicedesk.preprocess.images import sample_background

__all__ = [
    "MAX_TEXT_LAYER_WORDS_PER_PAGE",
    "PdfWord",
    "RenderedPage",
    "open_sanitized",
    "render_page",
    "text_layer_words",
]

_ACTION_KEYS = ("JS", "JavaScript", "OpenAction", "AA")
#: Active-content name tokens (not ``/JSans``-style font names: the token must end there), except
#: keys we already set to ``null``.
_ACTIVE_RE = re.compile(r"/(?:JavaScript|JS|Launch)(?![A-Za-z0-9#_.-])(?!\s*null\b)")
# MuPDF stext char_flags (PyMuPDF >= 1.25): glyph is filled / stroked. Neither = invisible (Tr 3).
_FILLED, _STROKED = 16, 32


def open_sanitized(data: bytes) -> tuple[pymupdf.Document, SanitizeReport]:  # noqa: PLR0915
    """Open, enforce page limits, strip active content and return a re-parsed clean document."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise RejectedInput("The PDF could not be read.") from exc
    if doc.needs_pass and not doc.authenticate(""):
        raise RejectedInput("Password-protected PDFs are not supported.")
    if doc.page_count < 1:
        raise RejectedInput("The PDF has no pages.")
    if doc.page_count > MAX_PAGES:
        raise RejectedInput(f"The PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.")
    report = SanitizeReport()
    if doc.is_repaired:
        report.notes.append("PDF structure was damaged and repaired on open")

    cat = doc.pdf_catalog()
    # JavaScript and automatic actions, in any object.
    js_hits = 0
    for xref in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(xref)
        except Exception:  # noqa: S112 - free/broken objects are skipped
            continue
        for key in keys:
            if key in _ACTION_KEYS:
                val = doc.xref_get_key(xref, key)
                if key in ("JS", "JavaScript") or _ACTIVE_RE.search(str(val)):
                    js_hits += 1
                doc.xref_set_key(xref, key, "null")
            elif key == "A" and _ACTIVE_RE.search(str(doc.xref_get_key(xref, "A")[1])):
                # Direct /A action dicts (outline items, link annots) holding JS/Launch.
                js_hits += 1
                doc.xref_set_key(xref, "A", "null")
        if "S" in keys and doc.xref_get_key(xref, "S")[1] in ("/JavaScript", "/Launch"):
            js_hits += 1
            doc.xref_set_key(xref, "S", "/GoTo")
    # Document-level JavaScript name tree when /Names is a direct dict inside the catalog (the
    # per-object key scan above only sees top-level keys of indirect objects).
    if doc.xref_get_key(cat, "Names/JavaScript")[0] != "null":
        doc.xref_set_key(cat, "Names/JavaScript", "null")
        js_hits += 1
    if js_hits:
        report.removed_javascript = True
        report.notes.append(f"removed {js_hits} JavaScript/launch action(s)")

    # Embedded files.
    names = list(doc.embfile_names())
    for name in names:
        doc.embfile_del(name)
    if names:
        report.removed_embedded_files = True
        report.notes.append(f"removed {len(names)} embedded file(s)")

    # Forms (widgets) and annotations.
    widgets = annots = links = 0
    for page in doc:
        for w in list(page.widgets() or []):
            page.delete_widget(w)  # type: ignore[arg-type]
            widgets += 1
        for a in list(page.annots() or []):
            page.delete_annot(a)
            annots += 1
        for link in page.get_links():
            page.delete_link(link)
            links += 1
    if doc.xref_get_key(cat, "AcroForm")[0] != "null":
        doc.xref_set_key(cat, "AcroForm", "null")
        report.removed_forms = True
    if widgets:
        report.removed_forms = True
        report.notes.append(f"removed {widgets} form field(s)")
    if annots or links:
        report.notes.append(f"removed {annots} annotation(s) and {links} link(s)")
    if doc.xref_get_key(cat, "Names/EmbeddedFiles")[0] != "null":
        doc.xref_set_key(cat, "Names/EmbeddedFiles", "null")
        report.removed_embedded_files = True

    # Re-serialize with garbage collection so removed objects are really gone, then re-open.
    clean = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    try:
        out = pymupdf.open(stream=clean, filetype="pdf")
    except Exception as exc:  # pragma: no cover - MuPDF re-parsing its own output
        raise RejectedInput("The PDF could not be read.") from exc
    # Fail closed: if any active-content marker survived (nested inline dicts we did not reach),
    # reject rather than pass the file on.
    for xref in range(1, out.xref_length()):
        try:
            obj = out.xref_object(xref, compressed=True)
        except Exception:  # noqa: S112 - free/broken objects are skipped
            continue
        if _ACTIVE_RE.search(obj):
            close_quietly(out)
            raise RejectedInput("The PDF contains active content that could not be removed.")
    return out, report


@dataclass
class RenderedPage:
    index: int
    image: Image.Image  # RGB, long edge == TARGET_LONG_EDGE
    scale: float  # processed px per PDF point
    sx: float
    sy: float


def render_page(page: pymupdf.Page, target: int = TARGET_LONG_EDGE) -> RenderedPage:
    rect = page.rect  # rotation applied
    if rect.width <= 0 or rect.height <= 0:
        raise RejectedInput("The PDF has an empty page.")
    zoom = target / max(rect.width, rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if max(img.size) != target:
        # MuPDF rounds the pixmap outward; force the exact long edge.
        if rect.width >= rect.height:
            size = (target, max(1, round(rect.height * zoom)))
        else:
            size = (max(1, round(rect.width * zoom)), target)
        img = img.resize(size, Image.Resampling.LANCZOS)
    sx = img.width / rect.width
    sy = img.height / rect.height
    return RenderedPage(index=page.number or 0, image=img, scale=zoom, sx=sx, sy=sy)


@dataclass
class PdfWord:
    span: TextLayerSpan
    invisible: bool = False  # render mode 3 or alpha 0
    notes: list[str] = field(default_factory=list)


def _rgb(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


#: Real invoices have well under 1,000 words a page; far more is a resource-exhaustion attempt.
MAX_TEXT_LAYER_WORDS_PER_PAGE = 5000


def _transform(
    m: tuple[float, float, float, float, float, float], r: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Bounding box of rect ``r`` under affine matrix ``m`` (PDF ``a b c d e f`` order)."""
    a, b, c, d, e, f = m
    xs: list[float] = []
    ys: list[float] = []
    for x in (r[0], r[2]):
        for y in (r[1], r[3]):
            xs.append(a * x + c * y + e)
            ys.append(b * x + d * y + f)
    return (min(xs), min(ys), max(xs), max(ys))


def text_layer_words(page: pymupdf.Page, rendered: RenderedPage) -> list[PdfWord]:
    """Words of the page's text layer with size, fill colour, off-page flag and the background
    colour sampled from the render, boxes in processed-image px. Text outside the page is kept
    (no mediabox clip) and marked ``off_page``."""
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_MEDIABOX_CLIP
    raw: dict[str, Any] = page.get_text(  # type: ignore[assignment]
        "rawdict", flags=flags, clip=pymupdf.INFINITE_RECT()
    )
    m = page.rotation_matrix
    rot = (m.a, m.b, m.c, m.d, m.e, m.f)
    px0, py0, px1, py1 = page.rect
    arr = np.asarray(rendered.image)
    out: list[PdfWord] = []

    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            cur_chars: list[tuple[str, tuple[float, float, float, float]]] = []
            cur_style: tuple[float, int, str, bool] | None = None

            def flush(
                chars: list[tuple[str, tuple[float, float, float, float]]],
                style: tuple[float, int, str, bool] | None,
            ) -> None:
                if not chars or style is None:
                    return
                if len(out) >= MAX_TEXT_LAYER_WORDS_PER_PAGE:
                    # Fail closed: truncating would hide text from the hidden-text detector.
                    raise RejectedInput("The PDF has an unusually large amount of text.")
                text = "".join(c for c, _ in chars)
                x0 = min(b[0] for _, b in chars)
                y0 = min(b[1] for _, b in chars)
                x1 = max(b[2] for _, b in chars)
                y1 = max(b[3] for _, b in chars)
                # into rotated page space (what the render shows)
                rx0, ry0, rx1, ry1 = _transform(rot, (x0, y0, x1, y1))
                ix = min(rx1, px1) - max(rx0, px0)
                iy = min(ry1, py1) - max(ry0, py0)
                area = max((rx1 - rx0) * (ry1 - ry0), 1e-9)
                off = ix <= 0 or iy <= 0 or (ix * iy) / area < 0.5
                box: Box = (
                    math.floor(rx0 * rendered.sx),
                    math.floor(ry0 * rendered.sy),
                    math.ceil(rx1 * rendered.sx),
                    math.ceil(ry1 * rendered.sy),
                )
                bg = None if off else sample_background(arr, box)
                size, color, _font, invisible = style
                out.append(
                    PdfWord(
                        span=TextLayerSpan(
                            page=rendered.index,
                            text=text,
                            box=box,
                            font_size_pt=round(float(size), 2),
                            fill_rgb=_rgb(color),
                            background_rgb=bg,
                            off_page=off,
                        ),
                        invisible=invisible,
                    )
                )

            for span in line.get("spans", []):
                cflags = span.get("char_flags")
                invisible = (
                    cflags is not None and not (cflags & (_FILLED | _STROKED))
                ) or span.get("alpha", 255) == 0
                style = (
                    float(span.get("size", 0.0)),
                    int(span.get("color", 0)),
                    str(span.get("font", "")),
                    invisible,
                )
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c or c.isspace():
                        flush(cur_chars, cur_style)
                        cur_chars, cur_style = [], None
                        continue
                    if cur_style is not None and cur_style != style:
                        flush(cur_chars, cur_style)
                        cur_chars = []
                    cur_style = style
                    cur_chars.append((c, tuple(ch["bbox"])))  # type: ignore[arg-type]
            flush(cur_chars, cur_style)
    return out


def close_quietly(doc: pymupdf.Document) -> None:
    with contextlib.suppress(Exception):
        doc.close()
