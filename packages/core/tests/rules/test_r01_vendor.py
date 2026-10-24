from __future__ import annotations

from collections.abc import Callable
from typing import Any

from invoicedesk.config.tenant import RulesConfig, VendorConfig
from invoicedesk.contracts.rules import DocumentInfo, RuleContext, RuleResult
from invoicedesk.contracts.tenant_data import ApprovalRecord, TenantData
from invoicedesk.rules import r01_vendor
from invoicedesk.rules.vendor_match import resolve_vendor
from invoicedesk.testing.builders import make_extraction, text

CtxFactory = Callable[..., RuleContext]
Universe = Callable[..., TenantData]


def r1(make_ctx: CtxFactory, **kw: Any) -> RuleResult:
    return r01_vendor.check(make_ctx(**kw))


def test_exact_legal_name_and_alias(make_ctx: CtxFactory) -> None:
    res = r1(make_ctx)
    assert res.passed and res.severity == "pass"
    assert res.details["vendor_id"] == "v-rr" and res.details["method"] == "exact"
    for name in ("Rapid Rooter", "RAPID ROOTER PLUMBING, L.L.C.", "rapid rooter plumbing"):
        res = r1(make_ctx, vendor_name=text(name))
        assert res.passed, name
        assert res.details["vendor_id"] == "v-rr"


def test_lookalike_edit_distance_is_flagged(make_ctx: CtxFactory) -> None:
    for name in ("Rapid Rotor", "Rapid Rotor Plumbing LLC", "Rapid Roter Plumbing"):
        res = r1(make_ctx, vendor_name=text(name))
        assert not res.passed and res.severity == "flag", name
        assert res.details["method"] == "edit_distance"
        assert res.details["vendor_id"] is None  # a lookalike never resolves to the real vendor
        assert res.details["resembles_vendor_id"] == "v-rr"
        assert res.details["badge"] == "lookalike"
        assert "Rapid Rooter Plumbing LLC" in res.message
        assert res.fields == ["vendor_name"]


def test_homoglyph_is_flagged(make_ctx: CtxFactory) -> None:
    # Cyrillic "а" and "о"; ASCII digit zero; "rn" for "m"-style tricks are covered in _text.
    for name in ("Rаpid Rооter Plumbing", "Rapid R00ter Plumbing LLC", "Rapid Rooter P1umbing"):
        res = r1(make_ctx, vendor_name=text(name))
        assert not res.passed and res.severity == "flag", name
        assert res.details["method"] == "homoglyph"
        assert "lookalike characters" in res.message


def test_mixed_script_without_close_match_is_flagged(make_ctx: CtxFactory) -> None:
    res = r1(make_ctx, vendor_name=text("Zебra Landscaping Services"))
    assert res.severity == "flag" and res.details["method"] == "mixed_script"
    assert res.details["resembles_vendor_id"] is not None


def test_mixed_script_with_empty_vendor_master(make_ctx: CtxFactory, universe: Universe) -> None:
    res = r1(make_ctx, vendor_name=text("Zебra"), tenant=universe(vendors=()))
    assert res.severity == "flag"
    assert res.details["resembles_vendor_id"] is None
    assert "a vendor name" in res.message


def test_homoglyph_checks_can_be_disabled(make_ctx: CtxFactory, config: RulesConfig) -> None:
    cfg = config.model_copy(
        update={"vendor": VendorConfig(check_homoglyphs=False, lookalike_max_edit_distance=0)}
    )
    res = r1(make_ctx, vendor_name=text("Zебra Landscaping Services"), cfg=cfg)
    assert res.severity == "review" and res.details["method"] == "unknown"
    # homoglyph skeleton collision is then caught only if edit distance allows it
    res = r1(make_ctx, vendor_name=text("Rapid R00ter Plumbing"), cfg=cfg)
    assert res.details["method"] != "homoglyph"


def test_unknown_vendor_needs_review(make_ctx: CtxFactory) -> None:
    res = r1(make_ctx, vendor_name=text("Bluewater Window Cleaning"))
    assert not res.passed and res.severity == "review"
    assert res.details["badge"] == "new_vendor" and res.details["vendor_id"] is None
    assert "not in the vendor list" in res.message


def test_unknown_vendor_with_empty_master(make_ctx: CtxFactory, universe: Universe) -> None:
    res = r1(make_ctx, vendor_name=text("Bluewater"), tenant=universe(vendors=()))
    assert res.severity == "review" and res.details["method"] == "unknown"


def test_missing_vendor_name(make_ctx: CtxFactory) -> None:
    for v in (None, "   ", "LLC"):
        res = r1(make_ctx, vendor_name=text(v, 0.2))
        assert res.severity == "review"
    assert "missing" in r1(make_ctx, vendor_name=text(None)).message


def test_fuzzy_match_corroborated_by_tax_id_or_address(make_ctx: CtxFactory) -> None:
    name = text("Rapid Rooter Plumbing and Drain Services")
    res = r1(make_ctx, vendor_name=name)  # builder has tax 4821 + matching address
    assert res.passed and res.details["method"] == "fuzzy"
    assert res.details["vendor_id"] == "v-rr"
    assert res.details["evidence"] == {"tax_id_last4": True, "address": True}

    res = r1(make_ctx, vendor_name=name, vendor_tax_id_last4=text(None))
    assert res.passed and res.details["evidence"]["address"] is True


def test_fuzzy_name_without_corroboration_is_new_vendor(make_ctx: CtxFactory) -> None:
    res = r1(
        make_ctx,
        vendor_name=text("Rapid Rooter Plumbing and Drain Services"),
        vendor_tax_id_last4=text("9999"),
        vendor_address=text("1 Somewhere Else Blvd"),
    )
    assert res.severity == "review"
    assert res.details["resembles_vendor_id"] == "v-rr"
    assert "resembles" in res.message


def test_fuzzy_candidate_without_address_on_file(make_ctx: CtxFactory) -> None:
    # Harbor has no address on file and no tax id: nothing can corroborate.
    res = r1(
        make_ctx, vendor_name=text("Harbor Paint and Drywall Company"), vendor_address=text(None)
    )
    assert res.severity == "review" and res.details["method"] == "unknown"


def test_new_vendor_approval_clears_review(make_ctx: CtxFactory) -> None:
    ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    res = r1(make_ctx, vendor_name=text("Bluewater Window Cleaning"), approvals=(ok,))
    assert res.passed and "approved" in res.message


def test_new_vendor_approval_by_uploader_or_rejected_does_not_clear(make_ctx: CtxFactory) -> None:
    self_ok = ApprovalRecord(approver_id="u-up", kind="new_vendor", decision="approve")
    other_ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    rej = ApprovalRecord(approver_id="u-cfo", kind="new_vendor", decision="reject")
    wrong_kind = ApprovalRecord(approver_id="u-boss", kind="over_cap", decision="approve")
    for approvals in ((self_ok,), (other_ok, rej), (wrong_kind,)):
        res = r1(make_ctx, vendor_name=text("Bluewater Window Cleaning"), approvals=approvals)
        assert res.severity == "review", approvals
        assert res.details["headline"] == (
            "New vendor: Bluewater Window Cleaning is not in the vendor list"
        )


def test_approval_never_clears_a_lookalike(make_ctx: CtxFactory) -> None:
    ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    res = r1(make_ctx, vendor_name=text("Rapid Rotor"), approvals=(ok,))
    assert res.severity == "flag"


def test_blocked_vendor_is_flagged(make_ctx: CtxFactory) -> None:
    ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    res = r1(
        make_ctx,
        vendor_name=text("Northgate Builders"),
        vendor_tax_id_last4=text(None),
        approvals=(ok,),
    )
    assert res.severity == "flag" and "blocked" in res.message


def test_pending_vendor_needs_review_until_approved(make_ctx: CtxFactory) -> None:
    kw = {"vendor_name": text("Harbor Paint Co"), "vendor_tax_id_last4": text(None)}
    res = r1(make_ctx, **kw)
    assert res.severity == "review" and "pending" in res.message
    assert res.details["vendor_id"] == "v-pend"
    ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    assert r1(make_ctx, approvals=(ok,), **kw).passed


def test_tax_id_mismatch_on_matched_vendor(make_ctx: CtxFactory) -> None:
    res = r1(make_ctx, vendor_tax_id_last4=text("Tax ID 12-3459999"))
    assert res.severity == "review"
    assert res.fields == ["vendor_name", "vendor_tax_id_last4"]
    assert "9999" in res.message and "4821" in res.message


def test_tax_id_ignored_when_vendor_has_none(make_ctx: CtxFactory) -> None:
    res = r1(make_ctx, vendor_name=text("Harbor Paint Co"), vendor_tax_id_last4=text("1234"))
    assert res.severity == "review" and "pending" in res.message


def test_uploader_none_any_approver_counts(make_ctx: CtxFactory) -> None:
    ok = ApprovalRecord(approver_id="u-boss", kind="new_vendor", decision="approve")
    doc = DocumentInfo(document_id="d", sha256="s", source="email")
    res = r1(make_ctx, vendor_name=text("Bluewater"), approvals=(ok,), document=doc)
    assert res.passed


def test_resolve_vendor_prefers_exact_over_lookalike(
    config: RulesConfig, universe: Universe
) -> None:
    # "Oakline Electric" exactly matches OAK even though other vendors are within distance.
    m = resolve_vendor(make_extraction(vendor_name=text("Oakline Electric")), universe(), config)
    assert m.method == "exact" and m.resolved and not m.lookalike
