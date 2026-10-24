"""R9 confidence: every required field must be present and at or above its calibrated
threshold (``config.confidence.threshold(name)``). A required field with no calibrated
probability fails closed: the raw model self-report never clears this rule."""

from __future__ import annotations

from invoicedesk.contracts.extraction import InvoiceExtraction, field_value
from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.rules._common import FunctionRule, fail, passed

RULE_ID = "R9"
NAME = "confidence"

_LABELS = {
    "vendor_name": "vendor name",
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "work_order_number": "work-order number",
    "property_or_unit": "property or unit",
}


def _value(ex: InvoiceExtraction, name: str) -> object:
    try:
        return field_value(ex, name)
    except AttributeError:
        return None


def check(ctx: RuleContext) -> RuleResult:
    ccfg = ctx.config.confidence
    low: list[str] = []
    parts: list[str] = []
    scores: dict[str, float | None] = {}
    for name in ccfg.required_fields:
        label = _LABELS.get(name, name.replace("_", " "))
        tau = ccfg.threshold(name)
        conf = ctx.calibrated_conf.get(name)
        scores[name] = conf
        value = _value(ctx.extraction, name)
        if value is None or value == "":
            low.append(name)
            parts.append(f"{label} is missing or unreadable")
        elif conf is None:
            low.append(name)
            parts.append(f"{label} has no calibrated confidence")
        elif conf < tau:
            low.append(name)
            parts.append(f"{label} confidence {conf:.2f} is below {tau:.2f}")

    if low:
        msg = "Please confirm: " + "; ".join(parts) + "."
        return fail(
            RULE_ID, NAME, "review", msg, fields=low, badge="low_confidence", calibrated=scores
        )
    return passed(RULE_ID, NAME, "All required fields are confident.", calibrated=scores)


RULE = FunctionRule(RULE_ID, NAME, "review", check)
