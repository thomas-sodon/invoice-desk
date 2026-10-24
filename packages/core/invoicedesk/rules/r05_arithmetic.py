"""R5 arithmetic: sum of lines = subtotal; subtotal + tax = total; tax rate plausible;
quantity x unit price = line amount. Integer cents throughout (quantities parse to Decimal)."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.money import format_cents, pct_bps_of
from invoicedesk.rules._common import FunctionRule, fail, passed

RULE_ID = "R5"
NAME = "arithmetic"

_QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def parse_quantity(text: str | None) -> Decimal | None:
    """Leading number of a quantity cell (``"2"``, ``"1.5 hr"``); ``None`` if absent."""
    if not text:
        return None
    m = _QTY_RE.match(text)
    return Decimal(m.group(1)) if m else None


def _fmt_bps(bps: int) -> str:
    return f"{bps // 100}.{bps % 100:02d}%"


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    cfg = ctx.config.arithmetic
    slack = cfg.rounding_slack_cents
    problems: list[str] = []
    fields: list[str] = []

    total = ex.total.value_cents
    subtotal = ex.subtotal.value_cents
    tax = ex.tax.value_cents if ex.tax.value_cents is not None else 0
    amounts = [li.amount.value_cents for li in ex.line_items]
    line_sum = (
        sum(a for a in amounts if a is not None)
        if amounts and all(a is not None for a in amounts)
        else None
    )

    if total is None:
        problems.append("the total is unreadable, so the arithmetic cannot be verified")
        fields.append("total")

    for i, li in enumerate(ex.line_items):
        qty = parse_quantity(li.quantity.value)
        unit = li.unit_price.value_cents
        amt = li.amount.value_cents
        if qty is None or unit is None or amt is None:
            continue
        expected = int((qty * unit).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if abs(expected - amt) > slack:
            problems.append(
                f"line {i + 1}: {li.quantity.value} x {format_cents(unit)} is "
                f"{format_cents(expected)}, not {format_cents(amt)}"
            )
            fields.append(f"line_items[{i}].amount")

    if line_sum is not None and subtotal is not None and abs(line_sum - subtotal) > slack:
        problems.append(
            f"line items add up to {format_cents(line_sum)}, but the subtotal says "
            f"{format_cents(subtotal)}"
        )
        fields.append("subtotal")

    base = subtotal if subtotal is not None else line_sum
    if base is not None and total is not None and abs(base + tax - total) > slack:
        label = "subtotal" if subtotal is not None else "line items"
        problems.append(
            f"{label} {format_cents(base)} + tax {format_cents(tax)} = "
            f"{format_cents(base + tax)}, but the total says {format_cents(total)}"
        )
        fields.append("total")

    rate_bps: int | None = None
    if base:
        rate_bps = pct_bps_of(abs(tax), abs(base))
        if tax * base < 0 or not (cfg.tax_rate_min_bps <= rate_bps <= cfg.tax_rate_max_bps):
            problems.append(
                f"tax rate {_fmt_bps(rate_bps)} is outside the expected "
                f"{_fmt_bps(cfg.tax_rate_min_bps)}-{_fmt_bps(cfg.tax_rate_max_bps)} range"
            )
            fields.append("tax")

    details: dict[str, Any] = {"line_sum_cents": line_sum, "tax_rate_bps": rate_bps}
    if problems:
        msg = "Arithmetic does not add up: " + "; ".join(problems) + "."
        fields = list(dict.fromkeys(fields))
        return fail(RULE_ID, NAME, "review", msg, fields=fields, badge="arithmetic", **details)
    return passed(RULE_ID, NAME, "Lines, subtotal, tax and total add up.", **details)


RULE = FunctionRule(RULE_ID, NAME, "review", check)
