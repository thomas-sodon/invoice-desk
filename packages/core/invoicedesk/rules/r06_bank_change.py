"""R6 bank_change (CLAUDE.md invariant 3). Always ``flag``, never cleared by this rule.

Fails when ANY of:

* remit-to / bank details are printed and their fingerprint differs from the vendor master's
  ``remit_to_hash`` (or there is no resolved vendor or no fingerprint on file to compare);
* the screening classifier reported category ``bank_or_remit_change`` (document or email body);
* the extractor quoted any ``embedded_instructions``;
* the document is a ``bank_change_letter``.

There is deliberately no config switch and no approval input here: release from Flagged needs
two approvers plus a callback attestation, enforced by ``invoicedesk.status.transition``. Every
number in the message and details is masked to its last 4.
"""

from __future__ import annotations

from typing import Any

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.masking import mask_number, mask_payload, mask_text
from invoicedesk.remit import extract_account_digits, remit_fingerprint
from invoicedesk.rules._common import FunctionRule, fail, passed
from invoicedesk.rules.vendor_match import resolve_vendor

RULE_ID = "R6"
NAME = "bank_change"
APPROVALS_NOTE = (
    "Two approvers (not the uploader) and a callback to the number on file are required."
)


def _mask_remit(text: str) -> str:
    """Mask every account-like token (5+ digits) to its last 4, as for a bank field."""
    return str(mask_payload({"remit_to_bank_details": text})["remit_to_bank_details"])


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    reasons: list[str] = []
    fields: list[str] = []
    details: dict[str, Any] = {"approvals_required": {"count": 2, "callback": True}}

    remit_text = ex.remit_to_bank_details.value
    fp = remit_fingerprint(remit_text)
    if fp is not None and remit_text is not None:
        _, account = extract_account_digits(remit_text)
        acct = f" (account {mask_number(account)})" if account else ""
        vm = resolve_vendor(ex, ctx.tenant, ctx.config)
        v = vm.vendor
        if v is None:
            reasons.append(
                f"the invoice carries remit-to bank details{acct} for a vendor that is not "
                "in the vendor list"
            )
        elif v.remit_to_hash is None:
            reasons.append(
                f"the invoice carries remit-to bank details{acct}, but {v.legal_name} has "
                "none on file to compare"
            )
        elif fp != v.remit_to_hash:
            on_file = f" (on file: account ending {v.bank_last4})" if v.bank_last4 else ""
            reasons.append(f"remit-to bank details{acct} differ from the vendor list{on_file}")
        details["remit_to_masked"] = _mask_remit(remit_text)
        details["remit_matches_master"] = v is not None and fp == v.remit_to_hash
        if reasons:
            fields.append("remit_to_bank_details")

    if "bank_or_remit_change" in ctx.screening.categories:
        reasons.append("the screening classifier found a bank or remit-to change request")
        details["screen_spans"] = [mask_text(s.text) for s in ctx.screening.flagged_spans]

    if ex.embedded_instructions:
        reasons.append(
            f"the document contains {len(ex.embedded_instructions)} embedded instruction(s) "
            "addressed to an automated system"
        )
        details["embedded_instructions"] = [mask_text(t) for t in ex.embedded_instructions]

    if ex.doc_type == "bank_change_letter":
        reasons.append("the document is a bank-change letter")

    if reasons:
        msg = mask_text("Bank-detail hold: " + "; ".join(reasons) + ". " + APPROVALS_NOTE) or ""
        return fail(RULE_ID, NAME, "flag", msg, fields=fields, badge="bank_change", **details)
    return passed(RULE_ID, NAME, "No bank or remit-to change.", **details)


RULE = FunctionRule(RULE_ID, NAME, "flag", check)
