"""R9 confidence: every required field must be present and at or above its calibrated
threshold (``config.confidence.threshold(name)``). A required field with no calibrated
probability fails closed: the raw model self-report never clears this rule. So does a calibrated
value that is not a finite probability in [0, 1] (NaN would otherwise compare as "not below"
the threshold and pass), and a value that is only whitespace counts as missing."""

from __future__ import annotations

import math

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
        if value is None or (isinstance(value, str) and not value.strip()):
            low.append(name)
            parts.append(f"{label} is missing or unreadable")
        elif conf is None:
            low.append(name)
            parts.append(f"{label} has no calibrated confidence")
        elif not (math.isfinite(conf) and 0.0 <= conf <= 1.0):
            low.append(name)
            parts.append(f"{label} has an invalid calibrated confidence")
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
