"""R1 vendor_known: fuzzy and normalized match to the vendor master (DESIGN §5.5).

* lookalike (homoglyph, mixed script, edit distance <= N) -> flag
* blocked vendor -> flag
* unknown / pending vendor, or tax-ID mismatch on a matched vendor -> review; an ``approve`` of
  kind ``new_vendor`` from someone other than the uploader clears an unknown or pending vendor.
"""

from __future__ import annotations

from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.rules._common import FunctionRule, approved_by_other, fail, passed
from invoicedesk.rules.vendor_match import resolve_vendor

RULE_ID = "R1"
NAME = "vendor_known"
_FIELDS = ["vendor_name"]


def check(ctx: RuleContext) -> RuleResult:
    m = resolve_vendor(ctx.extraction, ctx.tenant, ctx.config)
    name = ctx.extraction.vendor_name.value or ""
    base = {
        "vendor_id": m.vendor.id if m.vendor else None,
        "method": m.method,
        "resembles_vendor_id": m.resembles.id if m.resembles else None,
        "score": m.score,
        "distance": m.distance,
        "evidence": m.evidence,
    }

    if m.lookalike:
        what = {
            "homoglyph": "uses lookalike characters to imitate",
            "mixed_script": "mixes alphabets and resembles",
            "edit_distance": "is nearly identical to",
        }[m.method]
        target = f" approved vendor {m.resembles.legal_name!r}" if m.resembles else " a vendor name"
        return fail(
            RULE_ID,
            NAME,
            "flag",
            f"Lookalike vendor: {name!r} {what}{target} but does not match it. "
            "Possible impersonation.",
            fields=_FIELDS,
            badge="lookalike",
            **base,
        )

    if m.vendor is None:
        if approved_by_other(ctx, "new_vendor"):
            return passed(RULE_ID, NAME, f"New vendor {name!r} approved.", **base)
        if not name.strip():
            msg = "Vendor name is missing; the vendor cannot be checked against the vendor list."
        elif m.resembles is not None:
            msg = (
                f"New vendor: {name!r} is not in the vendor list (it resembles "
                f"{m.resembles.legal_name!r}, but the tax ID and address do not confirm it)."
            )
        else:
            msg = f"New vendor: {name!r} is not in the vendor list. Approve the vendor first."
        return fail(RULE_ID, NAME, "review", msg, fields=_FIELDS, badge="new_vendor", **base)

    v = m.vendor
    if v.status == "blocked":
        return fail(
            RULE_ID,
            NAME,
            "flag",
            f"Vendor {v.legal_name!r} is blocked in the vendor list.",
            fields=_FIELDS,
            badge="blocked_vendor",
            **base,
        )

    tax = (ctx.extraction.vendor_tax_id_last4.value or "").strip()
    if tax and v.tax_id_last4 and tax[-4:] != v.tax_id_last4:
        return fail(
            RULE_ID,
            NAME,
            "review",
            f"Tax ID on the invoice ends {tax[-4:]}, but {v.legal_name!r} has one ending "
            f"{v.tax_id_last4} on file.",
            fields=["vendor_name", "vendor_tax_id_last4"],
            badge="identity_mismatch",
            **base,
        )

    if v.status == "pending" and not approved_by_other(ctx, "new_vendor"):
        return fail(
            RULE_ID,
            NAME,
            "review",
            f"Vendor {v.legal_name!r} is pending approval in the vendor list.",
            fields=_FIELDS,
            badge="new_vendor",
            **base,
        )

    return passed(RULE_ID, NAME, f"Vendor matched: {v.legal_name}.", **base)


RULE = FunctionRule(RULE_ID, NAME, "flag", check)
