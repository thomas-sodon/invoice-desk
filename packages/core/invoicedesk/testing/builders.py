"""Builders for contract objects in tests (any component may use these)."""

from __future__ import annotations

from typing import Any

from invoicedesk.contracts.extraction import (
    DateField,
    InvoiceExtraction,
    LineItem,
    MoneyField,
    TextField,
    WordRef,
)
from invoicedesk.contracts.screening import ScreeningOutcome

__all__ = ["clean_screening", "date", "make_extraction", "money", "text"]

_NOREF = WordRef(word_ids=[], fallback_box=None)


def text(value: str | None, conf: float = 0.99, word_ids: list[int] | None = None) -> TextField:
    return TextField(
        value=value, confidence=conf, source=WordRef(word_ids=word_ids or [], fallback_box=None)
    )


def money(cents: int | None, conf: float = 0.99, word_ids: list[int] | None = None) -> MoneyField:
    return MoneyField(
        value_cents=cents,
        confidence=conf,
        source=WordRef(word_ids=word_ids or [], fallback_box=None),
    )


def date(iso: str | None, conf: float = 0.99) -> DateField:
    return DateField(value_iso=iso, confidence=conf, source=_NOREF)


def make_extraction(**overrides: Any) -> InvoiceExtraction:
    """A clean, internally consistent invoice: 2 lines, $1,150.00 + $90.00 tax = $1,240.00."""
    base: dict[str, Any] = {
        "doc_type": "invoice",
        "vendor_name": text("Rapid Rooter Plumbing LLC"),
        "vendor_address": text("118 Alder Way, Springfield"),
        "vendor_tax_id_last4": text("4821"),
        "invoice_number": text("INV-2231"),
        "invoice_date": date("2026-10-10"),
        "due_date": date("2026-11-09"),
        "work_order_number": text("WO-1187"),
        "property_or_unit": text("12B"),
        "line_items": [
            LineItem(
                description=text("Replace kitchen drain trap"),
                quantity=text("1"),
                unit_price=money(85_000),
                amount=money(85_000),
            ),
            LineItem(
                description=text("Labor 2h"),
                quantity=text("2"),
                unit_price=money(15_000),
                amount=money(30_000),
            ),
        ],
        "subtotal": money(115_000),
        "tax": money(9_000),
        "total": money(124_000),
        "remit_to_bank_details": text(None, 0.99),
        "handwriting_present": False,
        "legibility_issues": [],
        "embedded_instructions": [],
    }
    base.update(overrides)
    return InvoiceExtraction.model_validate(base)


def clean_screening() -> ScreeningOutcome:
    return ScreeningOutcome(results=[], quarantined=False)
