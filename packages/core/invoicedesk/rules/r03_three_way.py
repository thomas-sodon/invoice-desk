"""R3 three_way_match: invoice vs work-order scope vs approved estimate (DESIGN §5.5, §5.4).

The billed amount is compared **pre-tax** (the estimate is pre-tax): the invoice subtotal, or
the sum of line amounts when the subtotal is missing, or the total as a last resort. Tolerance
is ``max(tolerance_bps of the estimate, tolerance_min_cents)``, inclusive
(:func:`invoicedesk.money.within_tolerance`). A failure carries the deterministic variance
sentence. Line-level scope matching is reported in ``details`` for the reviewer.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.contracts.tenant_data import WorkOrder
from invoicedesk.money import apply_bps, within_tolerance
from invoicedesk.rules._common import (
    FunctionRule,
    fail,
    find_work_order,
    passed,
    vendor_display_name,
)
from invoicedesk.rules._text import basic_norm
from invoicedesk.rules.variance import variance_headline, variance_sentence
from invoicedesk.rules.vendor_match import resolve_vendor

RULE_ID = "R3"
NAME = "three_way_match"
_SCOPE_MATCH_MIN = 70


def billed_amount(ex: InvoiceExtraction) -> tuple[int | None, str]:
    """Pre-tax billed amount in cents and which field it came from."""
    if ex.subtotal.value_cents is not None:
        return ex.subtotal.value_cents, "subtotal"
    amounts = [li.amount.value_cents for li in ex.line_items]
    if amounts and all(a is not None for a in amounts):
        return sum(a for a in amounts if a is not None), "line_items"
    if ex.total.value_cents is not None:
        return ex.total.value_cents, "total"
    return None, "none"


def _line_scope(ex: InvoiceExtraction, wo: WorkOrder) -> list[dict[str, Any]]:
    refs = [basic_norm(e.description) for e in wo.estimate_lines] or [basic_norm(wo.scope)]
    out: list[dict[str, Any]] = []
    for i, li in enumerate(ex.line_items):
        desc = basic_norm(li.description.value or "")
        score = max(fuzz.token_set_ratio(desc, r) for r in refs) if desc else 0.0
        out.append({"line": i, "in_scope": score >= _SCOPE_MATCH_MIN, "score": score})
    return out


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    wo = find_work_order(ctx.tenant, ex.work_order_number.value)
    if wo is None:
        return fail(
            RULE_ID,
            NAME,
            "review",
            "No matching work order, so there is no approved estimate to compare against.",
            fields=["work_order_number"],
            badge="over_estimate",
        )
    billed, source = billed_amount(ex)
    if billed is None:
        return fail(
            RULE_ID,
            NAME,
            "review",
            f"The invoice amount is unreadable, so it cannot be matched to {wo.wo_number}.",
            fields=["subtotal", "total"],
            badge="over_estimate",
        )

    tcfg = ctx.config.three_way
    est = wo.approved_estimate_cents
    allowed = max(abs(apply_bps(est, tcfg.tolerance_bps)), tcfg.tolerance_min_cents)
    scope = _line_scope(ex, wo)
    unit = ctx.tenant.unit(wo.unit_id)
    unit_label = unit.label if unit else "?"
    vm = resolve_vendor(ex, ctx.tenant, ctx.config)
    assigned = ctx.tenant.vendor(wo.vendor_id)
    vendor_src = vm.vendor or assigned
    vendor = vendor_display_name(vendor_src.legal_name) if vendor_src else "Vendor"
    sentence_args = {
        "billed_cents": billed,
        "estimate_cents": est,
        "unit_label": unit_label,
        "wo_number": wo.wo_number,
    }
    details: dict[str, Any] = {
        "work_order_id": wo.id,
        "billed_cents": billed,
        "billed_source": source,
        "estimate_cents": est,
        "variance_cents": billed - est,
        "allowed_cents": allowed,
        "estimate_lines_cents": sum(e.amount_cents for e in wo.estimate_lines),
        "lines": scope,
        "out_of_scope_lines": [s["line"] for s in scope if not s["in_scope"]],
    }
    if within_tolerance(
        billed, est, pct_bps=tcfg.tolerance_bps, min_cents=tcfg.tolerance_min_cents
    ):
        return passed(
            RULE_ID,
            NAME,
            f"Within tolerance of the approved estimate for {wo.wo_number}.",
            **details,
        )
    sentence = variance_sentence(vendor=vendor, **sentence_args)
    return fail(
        RULE_ID,
        NAME,
        "review",
        sentence,
        fields=["total"] if source == "total" else ["subtotal", "total"],
        badge="over_estimate",
        variance_sentence=sentence,
        headline=variance_headline(**sentence_args),
        **details,
    )


RULE = FunctionRule(RULE_ID, NAME, "review", check)
