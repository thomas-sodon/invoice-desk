"""R10 injection: quarantined by screening, a hidden/faint-text finding, or a ``malicious``
classifier verdict -> flag. ``should_quarantine`` is recomputed so an inconsistent
``quarantined`` bit can only make this stricter, never looser. Quoted spans are masked."""

from __future__ import annotations

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.contracts.screening import should_quarantine
from invoicedesk.masking import mask_text
from invoicedesk.rules._common import FunctionRule, fail, passed

RULE_ID = "R10"
NAME = "injection"

_DETECTOR_LABEL = {
    "hidden_text": "hidden text",
    "faint_text": "faint text",
    "injection_classifier": "an instruction aimed at an automated system",
}


def check(ctx: RuleContext) -> RuleResult:
    scr = ctx.screening
    hits = [
        r
        for r in scr.results
        if (r.detector in ("hidden_text", "faint_text") and r.verdict != "clean")
        or (r.detector == "injection_classifier" and r.verdict == "malicious")
    ]
    if scr.quarantined or should_quarantine(scr.results):
        found = sorted({_DETECTOR_LABEL[r.detector] for r in hits}) or ["a screening finding"]
        spans = [mask_text(s.text) for r in hits for s in r.spans]
        quote = f' ("{spans[0]}")' if spans else ""
        return fail(
            RULE_ID,
            NAME,
            "flag",
            f"Quarantined: the document contains {' and '.join(found)}{quote}. "
            "It was not processed further and needs human review.",
            badge="injection",
            detectors=sorted({r.detector for r in hits}),
            spans=spans,
            categories=sorted(scr.categories),
        )
    suspicious = [r.detector for r in scr.results if r.verdict == "suspicious"]
    return passed(
        RULE_ID, NAME, "No hidden text or injection found.", suspicious_detectors=suspicious
    )


RULE = FunctionRule(RULE_ID, NAME, "flag", check)
