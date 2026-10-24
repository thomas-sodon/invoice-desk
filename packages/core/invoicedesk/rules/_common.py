"""Small pure helpers shared by the rule modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from invoicedesk.contracts.rules import RuleContext, RuleResult, Severity
from invoicedesk.contracts.tenant_data import TenantData, WorkOrder
from invoicedesk.rules._text import basic_norm, tokens, work_order_key

__all__ = [
    "FunctionRule",
    "approved_by_other",
    "fail",
    "find_work_order",
    "passed",
    "unit_matches",
    "vendor_display_name",
]


@dataclass(frozen=True, slots=True)
class FunctionRule:
    """Adapts a pure ``check(ctx) -> RuleResult`` function to the :class:`Rule` Protocol.

    ``fail_severity`` is the rule's worst outcome; the engine uses it to fail closed if a rule
    ever raises.
    """

    rule_id: str
    name: str
    fail_severity: Severity
    check: Callable[[RuleContext], RuleResult]

    def __call__(self, ctx: RuleContext) -> RuleResult:
        return self.check(ctx)


def passed(
    rule_id: str, name: str, message: str, *, fields: list[str] | None = None, **details: Any
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        name=name,
        passed=True,
        severity="pass",
        message=message,
        fields=fields or [],
        details=details,
    )


def fail(
    rule_id: str,
    name: str,
    severity: Severity,
    message: str,
    *,
    fields: list[str] | None = None,
    **details: Any,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        name=name,
        passed=False,
        severity=severity,
        message=message,
        fields=fields or [],
        details=details,
    )


def approved_by_other(ctx: RuleContext, kind: str) -> bool:
    """True if someone other than the uploader approved ``kind`` and nobody rejected it."""
    votes = [a for a in ctx.approvals if a.kind == kind]
    if any(a.decision == "reject" for a in votes):
        return False
    uploader = ctx.document.uploaded_by
    return any(a.decision == "approve" and a.approver_id != uploader for a in votes)


def find_work_order(tenant: TenantData, raw: str | None) -> WorkOrder | None:
    """Exact ``wo_number`` match first, then a unique normalized match (``wo 01187``)."""
    if raw is None:
        return None
    exact = tenant.work_order(raw.strip())
    if exact is not None:
        return exact
    key = work_order_key(raw)
    if key is None:
        return None
    hits = [w for w in tenant.work_orders if work_order_key(w.wo_number) == key]
    return hits[0] if len(hits) == 1 else None


def unit_matches(tenant: TenantData, wo: WorkOrder, text: str) -> tuple[bool, str]:
    """Does free text such as ``"Unit 12B, Maple Court"`` name the work order's unit?

    The unit label must appear as a token, and the text must not name a different property.
    Returns ``(ok, reason)``.
    """
    unit = tenant.unit(wo.unit_id)
    if unit is None:
        return False, "the work order's unit is not in the property list"
    label = "".join(tokens(unit.label))
    toks = tokens(text)
    if label not in toks and label != "".join(toks):
        return False, f"invoice names {text!r}, work order is for Unit {unit.label}"
    norm_text = basic_norm(text)
    for prop in tenant.properties:
        prop_name = basic_norm(prop.name)
        if prop.id != unit.property_id and prop_name and prop_name in norm_text:
            return False, f"invoice names property {prop.name!r}, not the work order's property"
    return True, ""


def vendor_display_name(legal_name: str) -> str:
    """``"Rapid Rooter Plumbing LLC"`` -> ``"Rapid Rooter Plumbing"`` (for sentences)."""
    words = legal_name.replace(",", " ").split()
    while len(words) > 1 and basic_norm(words[-1]) == "":
        words.pop()
    return " ".join(words)
