"""Arithmetic consistency signals for escalation and calibration (integer cents only).

These are *signals*, not the R5 rule: R5 (``invoicedesk.rules``) decides the routing outcome.
Here we only ask, per field, "is this value consistent with the others?" so the calibrator and
the escalation policy can use it. ``None`` means "cannot be checked".
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from invoicedesk.contracts.extraction import InvoiceExtraction

__all__ = ["arithmetic_failed", "arithmetic_signals", "parse_quantity"]

_QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def parse_quantity(text: str | None) -> Decimal | None:
    if not text:
        return None
    m = _QTY_RE.match(text)
    return Decimal(m.group(1)) if m else None


def arithmetic_signals(ex: InvoiceExtraction, *, slack_cents: int = 1) -> dict[str, bool | None]:
    sig: dict[str, bool | None] = {"subtotal": None, "tax": None, "total": None}
    amounts = [li.amount.value_cents for li in ex.line_items]
    line_sum = sum(a for a in amounts if a is not None) if amounts and None not in amounts else None
    subtotal, tax, total = ex.subtotal.value_cents, ex.tax.value_cents, ex.total.value_cents

    for i, li in enumerate(ex.line_items):
        qty = parse_quantity(li.quantity.value)
        unit, amt = li.unit_price.value_cents, li.amount.value_cents
        key = f"line_items[{i}].amount"
        if qty is None or unit is None or amt is None:
            sig[key] = None
            continue
        expected = int((qty * unit).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        sig[key] = abs(expected - amt) <= slack_cents

    if subtotal is not None and line_sum is not None:
        sig["subtotal"] = abs(line_sum - subtotal) <= slack_cents
    base = subtotal if subtotal is not None else line_sum
    if base is not None and total is not None:
        ok = abs(base + (tax or 0) - total) <= slack_cents
        sig["total"] = ok
        if tax is not None:
            sig["tax"] = ok
        if subtotal is not None:
            sig["subtotal"] = ok if sig["subtotal"] is None else (sig["subtotal"] and ok)
    return sig


def arithmetic_failed(ex: InvoiceExtraction, *, slack_cents: int = 1) -> bool:
    return any(v is False for v in arithmetic_signals(ex, slack_cents=slack_cents).values())
