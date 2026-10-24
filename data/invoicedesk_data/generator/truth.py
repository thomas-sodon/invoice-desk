"""Invoice truth: the gold label is sampled FIRST, then rendered (DESIGN §6.2 step 1).

:class:`InvoiceTruth` holds what is printed on the document (not what the vendor master says),
plus the planted defects and the outcome they imply. :meth:`InvoiceTruth.to_extraction` turns it
into an ``InvoiceExtraction``-compatible gold label.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from invoicedesk.contracts.extraction import (
    DateField,
    DocType,
    InvoiceExtraction,
    LineItem,
    MoneyField,
    TextField,
    WordRef,
)
from invoicedesk.status import DocStatus
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BoxGold",
    "ExpectedOutcome",
    "HiddenStyle",
    "HiddenText",
    "InvoiceTruth",
    "Medium",
    "Severity",
    "TruthLine",
    "expected_from_rules",
]

Medium = Literal["native", "scanned", "photo", "handwritten"]
Severity = Literal["review", "flag"]
HiddenStyle = Literal["white", "tiny", "offpage", "faint"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TruthLine(_M):
    description: str
    quantity: str
    unit_price_cents: int
    amount_cents: int


class HiddenText(_M):
    """Text a human reader does not see (adversarial). ``font_pt`` is the rendered size."""

    text: str
    style: HiddenStyle
    font_pt: float = 9.0
    #: For ``faint``: grey-level contrast against the paper, 0.05 = 5 %.
    contrast: float | None = None


class ExpectedOutcome(_M):
    status: DocStatus
    #: Planted rule failures ``{"R3": "review"}``; the rules engine must fail at least these.
    rules: dict[str, Severity] = Field(default_factory=dict)
    #: Screening detectors that must fire (``hidden_text``, ``faint_text``,
    #: ``injection_classifier``).
    detectors: list[str] = Field(default_factory=list)
    quarantined: bool = False
    headline: str = ""
    #: Classifier categories the email body or document should produce.
    categories: list[str] = Field(default_factory=list)


def expected_from_rules(
    rules: dict[str, Severity],
    *,
    detectors: list[str] | None = None,
    quarantined: bool = False,
    headline: str = "",
    categories: list[str] | None = None,
) -> ExpectedOutcome:
    if quarantined or any(s == "flag" for s in rules.values()):
        status = DocStatus.FLAGGED
    elif rules:
        status = DocStatus.NEEDS_REVIEW
    else:
        status = DocStatus.READY_TO_PAY
    return ExpectedOutcome(
        status=status,
        rules=dict(rules),
        detectors=list(detectors or []),
        quarantined=quarantined,
        headline=headline,
        categories=list(categories or []),
    )


class BoxGold(_M):
    """A gold box in processed-image pixels (1,600 px long edge)."""

    page: int = 0
    box: tuple[int, int, int, int]
    text: str  # the printed text inside the box


class InvoiceTruth(_M):
    doc_id: str
    set_name: str
    medium: Medium
    template: str
    accent: str = "#1f4e79"
    doc_type: DocType = "invoice"

    vendor_slug: str | None  # None for vendors not in the master (new vendor, lookalike)
    vendor_id: str | None  # resolved vendor id (gold for the "vendor_id" metric)
    vendor_name: str
    vendor_address: str | None
    vendor_tax_id_last4: str | None
    vendor_phone: str | None = None
    vendor_email: str | None = None

    invoice_number: str | None
    invoice_date: dt.date | None
    due_date: dt.date | None
    #: Canonical work-order number (``WO-1187``) and how it is printed.
    work_order_number: str | None
    work_order_printed: str | None = None
    property_or_unit: str | None

    bill_to_name: str = "Maple Row Property Management"
    bill_to_address: str = "PO Box 4410, Springfield, OR 97477"

    lines: list[TruthLine]
    subtotal_cents: int | None
    tax_cents: int | None
    tax_rate_bps: int = 0
    total_cents: int | None

    remit_text: str | None = None
    notes: list[str] = Field(default_factory=list)
    #: Visible text addressed to an AI/system (gold ``embedded_instructions``).
    visible_instructions: list[str] = Field(default_factory=list)
    hidden: list[HiddenText] = Field(default_factory=list)
    handwriting_present: bool = False
    illegible_fields: list[str] = Field(default_factory=list)
    letter_body: list[str] = Field(default_factory=list)  # bank-change letters

    defects: list[str] = Field(default_factory=list)
    expected: ExpectedOutcome
    #: Free-form generation parameters (augmentation seed, number format, ...).
    params: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ gold label
    def to_extraction(self, boxes: dict[str, BoxGold] | None = None) -> InvoiceExtraction:
        """Gold ``InvoiceExtraction``: values as printed, confidence 1.0, gold boxes as
        ``fallback_box`` (OCR word ids are run-specific, so gold carries none)."""
        boxes = boxes or {}

        def ref(name: str) -> WordRef:
            b = boxes.get(name)
            return WordRef(word_ids=[], fallback_box=list(b.box) if b else None)

        def text(name: str, value: str | None) -> TextField:
            return TextField(value=value, confidence=1.0, source=ref(name))

        def money(name: str, cents: int | None) -> MoneyField:
            return MoneyField(value_cents=cents, confidence=1.0, source=ref(name))

        def date(name: str, d: dt.date | None) -> DateField:
            return DateField(
                value_iso=d.isoformat() if d else None, confidence=1.0, source=ref(name)
            )

        lines = [
            LineItem(
                description=text(f"line_items.{i}.description", ln.description),
                quantity=text(f"line_items.{i}.quantity", ln.quantity),
                unit_price=money(f"line_items.{i}.unit_price", ln.unit_price_cents),
                amount=money(f"line_items.{i}.amount", ln.amount_cents),
            )
            for i, ln in enumerate(self.lines)
        ]
        instructions = [*self.visible_instructions, *(h.text for h in self.hidden)]
        return InvoiceExtraction(
            doc_type=self.doc_type,
            vendor_name=text("vendor_name", self.vendor_name),
            vendor_address=text("vendor_address", self.vendor_address),
            vendor_tax_id_last4=text("vendor_tax_id_last4", self.vendor_tax_id_last4),
            invoice_number=text("invoice_number", self.invoice_number),
            invoice_date=date("invoice_date", self.invoice_date),
            due_date=date("due_date", self.due_date),
            work_order_number=text("work_order_number", self.work_order_number),
            property_or_unit=text("property_or_unit", self.property_or_unit),
            line_items=lines,
            subtotal=money("subtotal", self.subtotal_cents),
            tax=money("tax", self.tax_cents),
            total=money("total", self.total_cents),
            remit_to_bank_details=text("remit_to_bank_details", self.remit_text),
            handwriting_present=self.handwriting_present,
            legibility_issues=[f"{f} is illegible" for f in self.illegible_fields],
            embedded_instructions=instructions,
        )
