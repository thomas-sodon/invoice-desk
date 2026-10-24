"""The deterministic rules engine: always runs R1..R10, in order, and never raises.

If a rule ever raises (a bug, or data the rule did not anticipate), the engine fails closed: it
records a failed result at that rule's worst severity, so routing holds the document for a
human instead of letting it through.
"""

from __future__ import annotations

from collections.abc import Sequence

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.rules import (
    r01_vendor,
    r02_work_order,
    r03_three_way,
    r04_duplicate,
    r05_arithmetic,
    r06_bank_change,
    r07_amount_cap,
    r08_dates,
    r09_confidence,
    r10_injection,
)
from invoicedesk.rules._common import FunctionRule
from invoicedesk.status import RoutingDecision, route

__all__ = ["ALL_RULES", "DeterministicRulesEngine", "resolved_vendor_id", "route_results"]

ALL_RULES: tuple[FunctionRule, ...] = (
    r01_vendor.RULE,
    r02_work_order.RULE,
    r03_three_way.RULE,
    r04_duplicate.RULE,
    r05_arithmetic.RULE,
    r06_bank_change.RULE,
    r07_amount_cap.RULE,
    r08_dates.RULE,
    r09_confidence.RULE,
    r10_injection.RULE,
)


class DeterministicRulesEngine:
    """Implements :class:`invoicedesk.contracts.rules.RulesEngine`."""

    def __init__(self, rules: Sequence[FunctionRule] = ALL_RULES) -> None:
        self.rules = tuple(rules)

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        results: list[RuleResult] = []
        for rule in self.rules:
            try:
                res = rule(ctx)
            except Exception as exc:  # fail closed, never crash the pipeline
                res = RuleResult(
                    rule_id=rule.rule_id,
                    name=rule.name,
                    passed=False,
                    severity=rule.fail_severity,
                    message=f"Rule {rule.rule_id} could not be evaluated; held for a human.",
                    details={"error": type(exc).__name__},
                )
            results.append(res)
        return results


def route_results(results: Sequence[RuleResult]) -> RoutingDecision:
    """Routing decision for a set of results (``Flagged`` > ``NeedsReview`` > ``ReadyToPay``)."""
    return route([r.outcome() for r in results])


def resolved_vendor_id(results: Sequence[RuleResult]) -> str | None:
    """The vendor R1 resolved (stored by the pipeline as the final ``vendor_id`` field)."""
    for r in results:
        if r.rule_id == "R1":
            vid = r.details.get("vendor_id")
            return str(vid) if vid is not None else None
    return None
