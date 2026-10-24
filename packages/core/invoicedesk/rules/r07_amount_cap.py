"""R7 amount_cap: total above the tenant cap -> review, regardless of confidence.

Cleared only by an ``approve`` of kind ``over_cap`` from someone other than the uploader (and
no ``reject``). An unreadable total cannot be checked, so it is held too (fail closed).
"""

from __future__ import annotations

from typing import Any

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.money import format_cents
from invoicedesk.rules._common import FunctionRule, approved_by_other, fail, passed

RULE_ID = "R7"
NAME = "amount_cap"


def check(ctx: RuleContext) -> RuleResult:
    cap = ctx.config.amount_cap_cents
    total = ctx.extraction.total.value_cents
    details: dict[str, Any] = {"cap_cents": cap, "total_cents": total}
    if total is None:
        return fail(
            RULE_ID,
            NAME,
            "review",
            f"The total is unreadable, so the {format_cents(cap)} approval limit "
            "cannot be checked.",
            fields=["total"],
            badge="over_cap",
            **details,
        )
    if total <= cap:
        return passed(RULE_ID, NAME, f"Total is within the {format_cents(cap)} limit.", **details)
    if approved_by_other(ctx, "over_cap"):
        return passed(
            RULE_ID,
            NAME,
            f"Total {format_cents(total)} is over the {format_cents(cap)} limit; approved.",
            approved=True,
            **details,
        )
    return fail(
        RULE_ID,
        NAME,
        "review",
        f"Total {format_cents(total)} is over the {format_cents(cap)} approval limit.",
        fields=["total"],
        badge="over_cap",
        **details,
    )


RULE = FunctionRule(RULE_ID, NAME, "review", check)
