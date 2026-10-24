"""Field mappings from SROIE / CORD labels to InvoiceDesk's vendor, date and total.

* SROIE Task 3 (Voxel51/scanned_receipts mirror, FiftyOne ``samples.json``): ``company`` ->
  vendor_name, ``date`` -> invoice_date (day-first, Malaysian receipts), ``total`` -> total.
* CORD v1 (naver-clova-ix/cord-v1, test split of 100): ``gt_parse.total.total_price`` -> total.
  CORD does not annotate the store name or the date, so those stay ``None`` and are excluded
  from CORD's field metrics.

Totals are integer minor units of the receipt's own currency (MYR sen for SROIE; IDR has no
minor unit in practice, so CORD totals are whole rupiah x 100). They are compared only with
values parsed the same way; there is no currency conversion.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from invoicedesk.contracts.extraction import InvoiceExtraction

__all__ = [
    "PublicRecord",
    "map_cord",
    "map_sroie",
    "normalize_date",
    "parse_total_cents",
    "to_gold_extraction",
]

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


@dataclass(frozen=True)
class PublicRecord:
    source: str  # "sroie" | "cord"
    id: str
    image: str  # path relative to the dataset cache directory
    vendor_name: str | None
    invoice_date: str | None  # ISO
    total_cents: int | None
    currency: str
    vendor_address: str | None = None  # SROIE Task 3 "address" (CORD has none)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _valid(y: int, m: int, d: int) -> str | None:
    if y < 100:
        y += 2000
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def normalize_date(text: str | None) -> str | None:
    """Day-first date strings -> ISO. ``None`` when unparseable (never guesses month-first,
    except when the day-first reading is impossible)."""
    if not text:
        return None
    s = text.strip()
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _valid(y, mo, d) or _valid(y, d, mo)
    m = re.search(r"(\d{1,2})[\s-]*([A-Za-z]{3})[A-Za-z]*[\s,-]*(\d{2,4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return _valid(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{2,4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return _valid(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def parse_total_cents(text: str | None, *, decimals: int = 2) -> int | None:
    """``"RM 9.00"`` / ``"$7.60"`` -> 760; with ``decimals=0`` (IDR) ``"60.000"`` -> 6000000.

    Exact (no floats). Thousands separators may be ``,`` or ``.``; with ``decimals=2`` a
    trailing group of exactly two digits after the last separator is the fraction."""
    if text is None:
        return None
    s = re.sub(r"[^\d.,-]", "", str(text))
    if not re.search(r"\d", s):
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    if decimals == 0:
        digits = re.sub(r"[.,]", "", s)
        return (-1 if neg else 1) * int(digits) * 100
    m = re.match(r"^(.*?)[.,](\d{1,2})$", s)
    if m:
        whole = re.sub(r"[.,]", "", m.group(1)) or "0"
        frac = m.group(2).ljust(2, "0")
        cents = int(whole) * 100 + int(frac)
    else:
        cents = int(re.sub(r"[.,]", "", s)) * 100
    return -cents if neg else cents


def _label(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return _label(v.get("label") or v.get("value") or v.get("text"))
    return str(v).strip() or None


def map_sroie(sample: dict[str, Any]) -> PublicRecord:
    """One FiftyOne sample dict from the Voxel51 SROIE mirror."""
    image = str(sample.get("filepath", ""))
    rid = image.rsplit("/", 1)[-1].rsplit(".", 1)[0] or str(sample.get("_id", ""))
    return PublicRecord(
        source="sroie",
        id=rid,
        image=image,
        vendor_name=_label(sample.get("company")),
        invoice_date=normalize_date(_label(sample.get("date"))),
        total_cents=parse_total_cents(_label(sample.get("total"))),
        currency="MYR",
        vendor_address=_label(sample.get("address")),
    )


def map_cord(row_idx: int, ground_truth: str | dict[str, Any], image: str) -> PublicRecord:
    """One CORD row: ``ground_truth`` is the JSON string with ``gt_parse``."""
    gt = json.loads(ground_truth) if isinstance(ground_truth, str) else ground_truth
    parse = gt.get("gt_parse", {})
    total = parse.get("total", {})
    if isinstance(total, list):
        total = total[0] if total else {}
    raw = total.get("total_price") if isinstance(total, dict) else None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    return PublicRecord(
        source="cord",
        id=f"cord-test-{row_idx:03d}",
        image=image,
        vendor_name=None,
        invoice_date=None,
        total_cents=parse_total_cents(raw, decimals=0) if raw is not None else None,
        currency="IDR",
    )


def to_gold_extraction(r: PublicRecord) -> InvoiceExtraction:
    """An ``InvoiceExtraction``-shaped gold label with only vendor, date and total set."""

    def ref() -> dict[str, Any]:
        return {"word_ids": [], "fallback_box": None}

    def text(v: str | None) -> dict[str, Any]:
        return {"value": v, "confidence": 1.0, "source": ref()}

    def money(v: int | None) -> dict[str, Any]:
        return {"value_cents": v, "currency": "USD", "confidence": 1.0, "source": ref()}

    def date(v: str | None) -> dict[str, Any]:
        return {"value_iso": v, "confidence": 1.0, "source": ref()}

    return InvoiceExtraction.model_validate(
        {
            "doc_type": "invoice",
            "vendor_name": text(r.vendor_name),
            "vendor_address": text(r.vendor_address),
            "vendor_tax_id_last4": text(None),
            "invoice_number": text(None),
            "invoice_date": date(r.invoice_date),
            "due_date": date(None),
            "work_order_number": text(None),
            "property_or_unit": text(None),
            "line_items": [],
            "subtotal": money(None),
            "tax": money(None),
            "total": money(r.total_cents),
            "remit_to_bank_details": text(None),
            "handwriting_present": False,
            "legibility_issues": [],
            "embedded_instructions": [],
        }
    )
