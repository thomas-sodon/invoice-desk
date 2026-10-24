"""R2 work_order_exists: the WO exists, is open, belongs to this vendor and the unit matches."""

from __future__ import annotations

from typing import Any

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.rules._common import FunctionRule, fail, find_work_order, passed, unit_matches
from invoicedesk.rules.vendor_match import resolve_vendor

RULE_ID = "R2"
NAME = "work_order_exists"


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    raw = ex.work_order_number.value
    if not raw or not raw.strip():
        return fail(
            RULE_ID,
            NAME,
            "review",
            "No work-order number on the invoice.",
            fields=["work_order_number"],
            badge="work_order",
        )
    wo = find_work_order(ctx.tenant, raw)
    if wo is None:
        return fail(
            RULE_ID,
            NAME,
            "review",
            f"Work order {raw} was not found.",
            fields=["work_order_number"],
            badge="work_order",
        )

    problems: list[str] = []
    fields: list[str] = []
    if wo.status != "open":
        problems.append(f"{wo.wo_number} is {wo.status}")
        fields.append("work_order_number")

    vm = resolve_vendor(ex, ctx.tenant, ctx.config)
    if vm.vendor is None:
        problems.append(f"the vendor could not be confirmed as the one assigned to {wo.wo_number}")
        fields.append("vendor_name")
    elif vm.vendor.id != wo.vendor_id:
        assigned = ctx.tenant.vendor(wo.vendor_id)
        who = assigned.legal_name if assigned else "another vendor"
        problems.append(f"{wo.wo_number} is assigned to {who}, not {vm.vendor.legal_name}")
        fields.append("vendor_name")

    unit_text = (ex.property_or_unit.value or "").strip()
    if not unit_text:
        problems.append("the invoice does not name a property or unit")
        fields.append("property_or_unit")
    else:
        ok, reason = unit_matches(ctx.tenant, wo, unit_text)
        if not ok:
            problems.append(reason)
            fields.append("property_or_unit")

    details: dict[str, Any] = {
        "work_order_id": wo.id,
        "wo_number": wo.wo_number,
        "wo_status": wo.status,
    }
    if problems:
        msg = f"Work order {wo.wo_number}: " + "; ".join(problems) + "."
        return fail(RULE_ID, NAME, "review", msg, fields=fields, badge="work_order", **details)
    return passed(RULE_ID, NAME, f"Work order {wo.wo_number} is open and matches.", **details)


RULE = FunctionRule(RULE_ID, NAME, "review", check)
