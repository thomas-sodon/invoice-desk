"""Fixture builders for preprocess tests. Every fixture is generated here (PyMuPDF, Pillow,
pillow-heif); nothing is checked in."""

from __future__ import annotations

import io
from collections.abc import Callable

import pymupdf
import pytest
from invoicedesk.contracts.preprocess import OcrWord
from invoicedesk.storage import InMemoryStorage
from PIL import Image

LETTER = (612, 792)
ZOOM = 1600 / 792  # letter portrait -> long edge 1,600 px

HEADER_LINES: list[tuple[tuple[float, float], str, float]] = [
    ((72, 100), "INVOICE INV-2231", 18),
    ((72, 150), "Maple Row Property Management", 13),
    ((72, 200), "Work order WO-1187 Unit 12B", 13),
    ((72, 250), "Amount due 1,240.00", 13),
]


def make_pdf(
    lines: list[tuple[tuple[float, float], str, float]] | None = None,
    *,
    extra: Callable[[pymupdf.Document, pymupdf.Page], None] | None = None,
    pages: int = 1,
    color: tuple[float, float, float] = (0, 0, 0),
) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=LETTER[0], height=LETTER[1])
        for pos, text, size in lines if lines is not None else HEADER_LINES:
            page.insert_text(pos, text, fontsize=size, color=color)
        if extra is not None:
            extra(doc, page)
    return doc.tobytes()


def expected_words(data: bytes, page_no: int = 0) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Known word positions in processed px (PDF word rect * zoom)."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[page_no]
    z = 1600 / max(page.rect.width, page.rect.height)
    return [
        (w[4], (round(w[0] * z), round(w[1] * z), round(w[2] * z), round(w[3] * z)))
        for w in page.get_text("words")
    ]


def pdf_to_png(data: bytes, *, dpi: int = 150) -> bytes:
    doc = pymupdf.open(stream=data, filetype="pdf")
    return doc[0].get_pixmap(dpi=dpi).tobytes("png")


def to_format(png: bytes, fmt: str, **kw: object) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


class FakeOcr:
    """Deterministic OCR: returns the words configured per (page, pass). The enhanced pass is
    recognised by the CLAHE PNG being grayscale."""

    name = "fake"

    def __init__(
        self,
        words: dict[int, list[tuple[str, tuple[int, int, int, int]]]] | None = None,
        enhanced: dict[int, list[tuple[str, tuple[int, int, int, int]]]] | None = None,
    ) -> None:
        self.words = words or {}
        self.enhanced = enhanced or {}
        self.calls: list[tuple[int, str]] = []

    def recognize(self, png: bytes, *, page: int) -> list[OcrWord]:
        mode = Image.open(io.BytesIO(png)).mode
        which = "enhanced" if mode == "L" else "normal"
        self.calls.append((page, which))
        src = self.enhanced if which == "enhanced" else self.words
        return [
            OcrWord(id=i, page=page, text=t, box=b, conf=0.9)
            for i, (t, b) in enumerate(src.get(page, []))
        ]


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def fx():
    import sys

    return sys.modules[__name__]
