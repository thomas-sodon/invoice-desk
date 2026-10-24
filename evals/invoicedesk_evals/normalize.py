"""Field normalizers for scoring (DESIGN §5.6 "normalized exact match").

Every header field is compared after the normalizer for its kind:

* money: integer cents (``MoneyField.value_cents``); never floats.
* dates: ISO ``YYYY-MM-DD``; a malformed date normalizes to ``None`` (it can never match).
* ``vendor_name``: the *resolved vendor_id* when the gold record has one and the run resolved
  one (R1's output); otherwise the casefolded alphanumeric name.
* ``work_order_number``: exact WO number. Separators, case and a leading ``WO``/``W.O.``
  prefix are ignored ("WO #1104" == "WO-1104" == "wo1104"); the digits must match exactly.
* ``vendor_tax_id_last4``: the last four digits.
* ``doc_type``: exact enum value.
* other text (address, invoice number, unit, remit-to): NFKC, casefold, punctuation removed,
  whitespace collapsed. Remit-to text is compared like any text but is never printed unmasked.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from invoicedesk.contracts.extraction import DATE_FIELDS, MONEY_FIELDS, parse_iso_date

__all__ = [
    "Normalized",
    "norm_date",
    "norm_digits_last4",
    "norm_text",
    "norm_work_order",
    "normalize_field",
]

Normalized = str | int | None

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WO_PREFIX = re.compile(r"^(?:work\s*order|w\.?\s*o\.?)\s*(?:no\.?|number|#)?\s*", re.I)


def norm_text(value: Any) -> str | None:
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).casefold()
    s = _NON_ALNUM.sub(" ", s).strip()
    return " ".join(s.split()) or None


def norm_work_order(value: Any) -> str | None:
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).strip()
    s = _WO_PREFIX.sub("", s)
    s = _NON_ALNUM.sub("", s.casefold())
    return s or None


def norm_digits_last4(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(c for c in str(value) if c.isdigit())
    return digits[-4:] or None


def norm_date(value: Any) -> str | None:
    if value is None:
        return None
    d = parse_iso_date(str(value).strip())
    return None if d is None else d.isoformat()


def norm_money(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"money must be integer cents, got {type(value).__name__}")
    return value


def normalize_field(name: str, value: Any) -> Normalized:
    """Normalize one header field value (not ``vendor_name``'s vendor-id path; see
    :func:`invoicedesk_evals.metrics.fields.score_header_fields`)."""
    if name in MONEY_FIELDS:
        return norm_money(value)
    if name in DATE_FIELDS:
        return norm_date(value)
    if name == "work_order_number":
        return norm_work_order(value)
    if name == "vendor_tax_id_last4":
        return norm_digits_last4(value)
    if name == "doc_type":
        return None if value is None else str(value)
    return norm_text(value)
