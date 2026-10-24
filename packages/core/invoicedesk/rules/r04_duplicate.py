"""R4 duplicate: every hit is a flag ("Duplicate of INV-xxxx").

Checks against ``TenantData.prior_invoices`` (historical bills and earlier documents):

* ``exact_file``: same file SHA-256;
* ``exact``: same vendor and invoice number, same (or unreadable) total;
* ``tweaked_amount``: same vendor and invoice number, different total;
* ``fuzzy``: same vendor, different invoice number, total within ``fuzzy_amount_bps`` and
  invoice dates within ``fuzzy_window_days``;
* ``image``: page-0 pHash Hamming distance <= ``phash_max_distance``.

Vendor-keyed checks need a resolved vendor (R1); file and image checks do not.
"""

from __future__ import annotations

from typing import Any

from invoicedesk.contracts.extraction import parse_iso_date
from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.money import apply_bps, format_cents
from invoicedesk.rules._common import FunctionRule, fail, passed
from invoicedesk.rules._text import invoice_number_key
from invoicedesk.rules.vendor_match import resolve_vendor

RULE_ID = "R4"
NAME = "duplicate"

_REASON = {
    "exact_file": "same file",
    "exact": "same vendor and invoice number",
    "fuzzy": "same vendor, amount, week",
    "image": "same image",
}


def phash_distance(a: str | None, b: str | None) -> int | None:
    """Hamming distance between two hex pHashes; ``None`` if either is missing or malformed."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return None


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    cfg = ctx.config.duplicate
    vm = resolve_vendor(ex, ctx.tenant, ctx.config)
    vendor_id = vm.vendor.id if vm.vendor else None
    inv_key = invoice_number_key(ex.invoice_number.value)
    total = ex.total.value_cents
    inv_date = parse_iso_date(ex.invoice_date.value_iso)

    hits: list[dict[str, Any]] = []
    for p in ctx.tenant.prior_invoices:
        kind: str | None = None
        reason = ""
        same_vendor = vendor_id is not None and p.vendor_id == vendor_id
        same_number = inv_key is not None and invoice_number_key(p.invoice_number) == inv_key
        if p.sha256 and p.sha256 == ctx.document.sha256:
            kind = "exact_file"
        elif same_vendor and same_number:
            if total is None or p.total_cents is None or p.total_cents == total:
                kind = "exact"
            else:
                kind = "tweaked_amount"
                reason = (
                    f"same invoice number, total changed from {format_cents(p.total_cents)} "
                    f"to {format_cents(total)}"
                )
        elif (
            same_vendor
            and total is not None
            and p.total_cents is not None
            and inv_date is not None
            and p.invoice_date is not None
            and abs(total - p.total_cents) <= abs(apply_bps(p.total_cents, cfg.fuzzy_amount_bps))
            and abs((inv_date - p.invoice_date).days) <= cfg.fuzzy_window_days
        ):
            kind = "fuzzy"
        else:
            dist = phash_distance(ctx.document.phash, p.phash)
            if dist is not None and dist <= cfg.phash_max_distance:
                kind = "image"
                reason = f"same image (pHash distance {dist})"
        if kind is not None:
            hits.append(
                {
                    "kind": kind,
                    "reason": reason or _REASON[kind],
                    "source": p.source,
                    "ref_id": p.ref_id,
                    "invoice_number": p.invoice_number,
                    "total_cents": p.total_cents,
                }
            )

    if not hits:
        return passed(RULE_ID, NAME, "No duplicate found.", vendor_id=vendor_id)
    first = hits[0]
    label = first["invoice_number"] or f"document {first['ref_id']}"
    return fail(
        RULE_ID,
        NAME,
        "flag",
        f"Duplicate of {label} ({first['reason']}).",
        fields=["invoice_number", "total"],
        badge="duplicate",
        duplicate_of=first["ref_id"],
        matches=hits,
        vendor_id=vendor_id,
    )


RULE = FunctionRule(RULE_ID, NAME, "flag", check)
