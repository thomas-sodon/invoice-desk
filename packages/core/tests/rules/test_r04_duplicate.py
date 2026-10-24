from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from invoicedesk.contracts.rules import DocumentInfo, RuleContext, RuleResult
from invoicedesk.contracts.tenant_data import PriorInvoice, TenantData
from invoicedesk.rules import r04_duplicate
from invoicedesk.rules.r04_duplicate import phash_distance
from invoicedesk.testing.builders import date, money, text

CtxFactory = Callable[..., RuleContext]
Universe = Callable[..., TenantData]


def prior(**kw: object) -> PriorInvoice:
    base: dict[str, object] = {
        "source": "document",
        "ref_id": "d-2231",
        "vendor_id": "v-rr",
        "invoice_number": "INV-2231",
        "total_cents": 124_000,
        "invoice_date": dt.date(2026, 10, 10),
        "phash": None,
        "sha256": None,
    }
    base.update(kw)
    return PriorInvoice.model_validate(base)


def run(
    make_ctx: CtxFactory, universe: Universe, *priors: PriorInvoice, **kw: object
) -> RuleResult:
    return r04_duplicate.check(make_ctx(tenant=universe(prior_invoices=priors), **kw))


def test_no_duplicate(make_ctx: CtxFactory) -> None:
    res = r04_duplicate.check(make_ctx())
    assert res.passed and res.details["vendor_id"] == "v-rr"


def test_exact_duplicate_demo_beat(make_ctx: CtxFactory, universe: Universe) -> None:
    res = run(make_ctx, universe, prior())
    assert not res.passed and res.severity == "flag"
    assert res.message == "Duplicate of INV-2231 (same vendor and invoice number)."
    assert res.details["duplicate_of"] == "d-2231" and res.details["badge"] == "duplicate"
    # normalized invoice numbers and an unreadable total still count as exact
    res = run(make_ctx, universe, prior(invoice_number="inv 002231"), total=money(None))
    assert res.details["matches"][0]["kind"] == "exact"
    res = run(make_ctx, universe, prior(total_cents=None))
    assert res.details["matches"][0]["kind"] == "exact"


def test_fuzzy_same_vendor_amount_week(make_ctx: CtxFactory, universe: Universe) -> None:
    p = prior(invoice_number="INV-2240", total_cents=123_500, invoice_date=dt.date(2026, 10, 4))
    res = run(make_ctx, universe, p)
    assert res.severity == "flag"
    assert res.message == "Duplicate of INV-2240 (same vendor, amount, week)."
    assert res.details["matches"][0]["kind"] == "fuzzy"
    # Demo beat: a re-sent INV-2231 under a new invoice number, same vendor, amount and week.
    res = run(make_ctx, universe, prior(), invoice_number=text("INV-2236"))
    assert res.message == "Duplicate of INV-2231 (same vendor, amount, week)."


def test_fuzzy_boundaries(make_ctx: CtxFactory, universe: Universe) -> None:
    # +/- 1 % of the prior total ($1,228.00 -> $12.28); 7 days inclusive.
    ok = prior(invoice_number="X1", total_cents=122_800, invoice_date=dt.date(2026, 10, 3))
    assert not run(make_ctx, universe, ok).passed
    too_far_amt = prior(invoice_number="X1", total_cents=122_700)
    assert run(make_ctx, universe, too_far_amt).passed
    too_far_date = prior(invoice_number="X1", invoice_date=dt.date(2026, 10, 2))
    assert run(make_ctx, universe, too_far_date).passed
    other_vendor = prior(invoice_number="X1", vendor_id="v-oak")
    assert run(make_ctx, universe, other_vendor).passed
    for missing in ({"total_cents": None}, {"invoice_date": None}):
        assert run(make_ctx, universe, prior(invoice_number="X1", **missing)).passed
    assert run(make_ctx, universe, prior(invoice_number="X1"), total=money(None)).passed
    assert run(make_ctx, universe, prior(invoice_number="X1"), invoice_date=date(None)).passed


def test_tweaked_amount(make_ctx: CtxFactory, universe: Universe) -> None:
    res = run(make_ctx, universe, prior(total_cents=142_000))
    assert res.severity == "flag"
    assert res.message == (
        "Duplicate of INV-2231 (same invoice number, total changed from $1,420.00 to $1,240.00)."
    )
    assert res.details["matches"][0]["kind"] == "tweaked_amount"


def test_phash_near_copy(make_ctx: CtxFactory, universe: Universe) -> None:
    doc = DocumentInfo(document_id="d", sha256="s", phash="0000ffff0000ffff")
    near = prior(ref_id="hb-9", vendor_id=None, invoice_number=None, phash="0000ffff0000ff3f")
    res = run(make_ctx, universe, near, document=doc)
    assert res.severity == "flag"
    assert res.message == "Duplicate of document hb-9 (same image (pHash distance 2))."
    far = prior(vendor_id=None, invoice_number=None, phash="ffff0000ffff0000")
    assert run(make_ctx, universe, far, document=doc).passed


def test_exact_file(make_ctx: CtxFactory, universe: Universe) -> None:
    doc = DocumentInfo(document_id="d", sha256="abc", phash=None)
    res = run(
        make_ctx, universe, prior(vendor_id=None, invoice_number="Z", sha256="abc"), document=doc
    )
    assert res.details["matches"][0]["kind"] == "exact_file"
    assert "same file" in res.message


def test_unresolved_vendor_skips_vendor_keyed_checks(
    make_ctx: CtxFactory, universe: Universe
) -> None:
    res = run(make_ctx, universe, prior(), vendor_name=text("Rapid Rotor"))
    assert res.passed and res.details["vendor_id"] is None


def test_missing_invoice_number(make_ctx: CtxFactory, universe: Universe) -> None:
    res = run(make_ctx, universe, prior(), invoice_number=text(None))
    # still the fuzzy check fires (same vendor, amount, week)
    assert res.details["matches"][0]["kind"] == "fuzzy"


def test_multiple_hits_reported(make_ctx: CtxFactory, universe: Universe) -> None:
    res = run(make_ctx, universe, prior(), prior(ref_id="hb-2", source="history"))
    assert len(res.details["matches"]) == 2


def test_phash_distance() -> None:
    assert phash_distance("ff", "fe") == 1
    assert phash_distance(None, "ff") is None
    assert phash_distance("ff", "") is None
    assert phash_distance("ff", "fff") is None
    assert phash_distance("zz", "ff") is None
