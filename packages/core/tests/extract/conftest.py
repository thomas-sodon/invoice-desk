"""Fixtures for extraction tests: a preprocessed clean invoice whose OCR words match
``testing.builders.make_extraction()``, and helpers to build tier outputs citing them."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from invoicedesk.contracts.extraction import DateField, InvoiceExtraction, WordRef
from invoicedesk.contracts.preprocess import OcrWord, PageImage, PreprocessedDocument
from invoicedesk.storage import InMemoryStorage
from invoicedesk.testing.builders import make_extraction, money, text

WORDS: list[str] = [
    "Rapid", "Rooter", "Plumbing", "LLC",  # 0-3
    "118", "Alder", "Way,", "Springfield",  # 4-7
    "4821",  # 8
    "INV-2231",  # 9
    "10/10/2026",  # 10
    "11/09/2026",  # 11
    "WO-1187",  # 12
    "12B",  # 13
    "Replace", "kitchen", "drain", "trap", "1", "$850.00", "$850.00",  # 14-20
    "Labor", "2h", "2", "$150.00", "$300.00",  # 21-25
    "$1,150.00",  # 26
    "$90.00",  # 27
    "$1,240.00",  # 28
]  # fmt: skip


def _box(i: int) -> tuple[int, int, int, int]:
    row, col = divmod(i, 6)
    x0, y0 = 60 + col * 180, 80 + row * 40
    return (x0, y0, x0 + 150, y0 + 26)


def make_doc(document_id: str = "doc-0001", pages: int = 1) -> PreprocessedDocument:
    words = [OcrWord(id=i, page=0, text=t, box=_box(i), conf=0.98) for i, t in enumerate(WORDS)]
    page_images = [
        PageImage(
            index=p,
            width=1237,
            height=1600,
            storage_key=f"t/{document_id}/page-{p}.png",
            sha256=hashlib.sha256(f"{document_id}-{p}".encode()).hexdigest(),
            scale=1.0,
        )
        for p in range(pages)
    ]
    return PreprocessedDocument(
        document_id=document_id,
        sha256=hashlib.sha256(document_id.encode()).hexdigest(),
        mime="application/pdf",
        is_native_pdf=False,
        pages=page_images,
        words=words,
        phash="0" * 16,
    )


def _d(iso: str | None, conf: float, ids: list[int]) -> DateField:
    return DateField(
        value_iso=iso, confidence=conf, source=WordRef(word_ids=ids, fallback_box=None)
    )


def clean_extraction(conf: float = 0.99, **overrides: Any) -> InvoiceExtraction:
    base = make_extraction()
    li = base.line_items
    data: dict[str, Any] = {
        "vendor_name": text("Rapid Rooter Plumbing LLC", conf, [0, 1, 2, 3]),
        "vendor_address": text("118 Alder Way, Springfield", conf, [4, 5, 6, 7]),
        "vendor_tax_id_last4": text("4821", conf, [8]),
        "invoice_number": text("INV-2231", conf, [9]),
        "invoice_date": _d("2026-10-10", conf, [10]),
        "due_date": _d("2026-11-09", conf, [11]),
        "work_order_number": text("WO-1187", conf, [12]),
        "property_or_unit": text("12B", conf, [13]),
        "line_items": [
            li[0].model_copy(
                update={
                    "description": text("Replace kitchen drain trap", conf, [14, 15, 16, 17]),
                    "quantity": text("1", conf, [18]),
                    "unit_price": money(85_000, conf, [19]),
                    "amount": money(85_000, conf, [20]),
                }
            ),
            li[1].model_copy(
                update={
                    "description": text("Labor 2h", conf, [21, 22]),
                    "quantity": text("2", conf, [23]),
                    "unit_price": money(15_000, conf, [24]),
                    "amount": money(30_000, conf, [25]),
                }
            ),
        ],
        "subtotal": money(115_000, conf, [26]),
        "tax": money(9_000, conf, [27]),
        "total": money(124_000, conf, [28]),
    }
    data.update(overrides)
    return make_extraction(**data)


def as_dict(ex: InvoiceExtraction) -> dict[str, Any]:
    return ex.model_dump(mode="json")


@pytest.fixture
def doc() -> PreprocessedDocument:
    return make_doc()


@pytest.fixture
def storage(doc: PreprocessedDocument) -> InMemoryStorage:
    s = InMemoryStorage()
    for p in doc.pages:
        s.put(p.storage_key, b"\x89PNG-fake-" + p.sha256.encode(), content_type="image/png")
    return s


@pytest.fixture
def clean() -> Callable[..., InvoiceExtraction]:
    return clean_extraction


@pytest.fixture(name="make_doc")
def make_doc_fixture() -> Callable[..., PreprocessedDocument]:
    return make_doc
