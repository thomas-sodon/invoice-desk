"""Human field corrections (M10, US-3): validation and deterministic overlay on an extraction.

A correction replaces one header field's value. Corrected values are human-verified, so their
calibrated confidence becomes 1.0 for R9. Bank / remit-to details can never be "corrected":
a bank change goes through the vendor master and the two-person release (invariant 3), so the
only way past R6 stays the approval flow.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from invoicedesk.contracts.extraction import (
    DATE_FIELDS,
    HEADER_FIELDS,
    MONEY_FIELDS,
    InvoiceExtraction,
    parse_iso_date,
)
from invoicedesk.money import parse_money_to_cents

__all__ = [
    "CORRECTABLE_FIELDS",
    "Correction",
    "InvalidCorrection",
    "apply_corrections_to_extraction",
    "normalize_correction",
]

#: Header fields a reviewer may correct (everything but bank details and the document type).
CORRECTABLE_FIELDS: frozenset[str] = frozenset(HEADER_FIELDS) - {
    "remit_to_bank_details",
    "doc_type",
}


class InvalidCorrection(ValueError):
    """The correction names an unknown/forbidden field or has an unparseable value."""


@dataclass(frozen=True, slots=True)
class Correction:
    field: str
    #: Normalized like a ``fields`` row: cents as an integer string, ISO date, or text.
    value: str | None


def normalize_correction(field: str, value: object) -> Correction:
    """Validate and normalize one ``{"field", "value"}`` pair from the API."""
    if field not in CORRECTABLE_FIELDS:
        raise InvalidCorrection(f"field {field!r} cannot be corrected")
    if value is None or (isinstance(value, str) and not value.strip()):
        return Correction(field, None)
    if isinstance(value, bool):
        raise InvalidCorrection(f"{field}: invalid value")
    if field in MONEY_FIELDS:
        if isinstance(value, int):
            return Correction(field, str(value))
        if not isinstance(value, str):
            raise InvalidCorrection(f"{field}: amounts are integer cents or a money string")
        s = value.strip()
        try:
            cents = int(s) if s.lstrip("-").isdigit() else parse_money_to_cents(s)
        except ValueError as exc:
            raise InvalidCorrection(f"{field}: not an amount") from exc
        return Correction(field, str(cents))
    if not isinstance(value, str):
        raise InvalidCorrection(f"{field}: expected text")
    s = value.strip()
    if field in DATE_FIELDS:
        if parse_iso_date(s) is None:
            raise InvalidCorrection(f"{field}: dates are YYYY-MM-DD")
        return Correction(field, s)
    if len(s) > 500:
        raise InvalidCorrection(f"{field}: value too long")
    return Correction(field, s)


def apply_corrections_to_extraction(
    extraction: InvoiceExtraction,
    corrections: Iterable[Correction],
    calibrated: Mapping[str, float],
) -> tuple[InvoiceExtraction, dict[str, float]]:
    """Overlay corrections (in order; later ones win). Returns the new extraction and the
    calibrated-confidence map with corrected fields at 1.0."""
    data: dict[str, Any] = extraction.model_dump(mode="json")
    conf = dict(calibrated)
    for c in corrections:
        if c.field not in CORRECTABLE_FIELDS:
            raise InvalidCorrection(f"field {c.field!r} cannot be corrected")
        f = dict(data[c.field])
        if c.field in MONEY_FIELDS:
            f["value_cents"] = None if c.value is None else int(c.value)
        elif c.field in DATE_FIELDS:
            f["value_iso"] = c.value
        else:
            f["value"] = c.value
        f["confidence"] = 1.0
        data[c.field] = f
        conf[c.field] = 1.0
    return InvoiceExtraction.model_validate(data), conf
