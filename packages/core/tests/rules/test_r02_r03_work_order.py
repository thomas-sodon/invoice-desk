from __future__ import annotations

from collections.abc import Callable
from typing import Any

from invoicedesk.contracts.rules import RuleContext
from invoicedesk.contracts.tenant_data import TenantData, Unit, WorkOrder
from invoicedesk.rules import r02_work_order, r03_three_way
from invoicedesk.rules.r03_three_way import billed_amount
from invoicedesk.testing.builders import make_extraction, money, text

CtxFactory = Callable[..., RuleContext]
Universe = Callable[..., TenantData]


def _with_wo(universe: Universe, **wo_kw: Any) -> TenantData:
    base = universe()
    wos = list(base.work_orders)
    w = wos[0].model_copy(update=wo_kw)
    return universe(work_orders=(w, *wos[1:]))


# ---------------------------------------------------------------- R2


def test_r2_pass(make_ctx: CtxFactory) -> None:
    res = r02_work_order.check(make_ctx())
    assert res.passed and res.details["work_order_id"] == "wo-1187"


def test_r2_normalized_wo_and_unit_forms(make_ctx: CtxFactory) -> None:
    for wo, unit in [
        ("wo 1187", "Unit 12B"),
        ("Work Order #01187", "Maple Court #12B"),
        ("1187", "12-B"),
        ("WO-1187", "Apt 12B, 400 Elm St"),
    ]:
        res = r02_work_order.check(
            make_ctx(work_order_number=text(wo), property_or_unit=text(unit))
        )
        assert res.passed, (wo, unit, res.message)


def test_r2_missing_or_unknown_wo(make_ctx: CtxFactory) -> None:
    for v in (None, "  "):
        res = r02_work_order.check(make_ctx(work_order_number=text(v)))
        assert res.severity == "review" and "No work-order number" in res.message
    res = r02_work_order.check(make_ctx(work_order_number=text("WO-9999")))
    assert res.severity == "review" and "not found" in res.message
    res = r02_work_order.check(make_ctx(work_order_number=text("WO")))  # no digits at all
    assert res.severity == "review" and "not found" in res.message


def test_r2_ambiguous_normalized_wo(make_ctx: CtxFactory, universe: Universe) -> None:
    base = universe()
    dup = base.work_orders[0].model_copy(update={"id": "wo-x", "wo_number": "WO-01187"})
    tenant = universe(work_orders=(*base.work_orders, dup))
    res = r02_work_order.check(make_ctx(tenant=tenant, work_order_number=text("wo 1187")))
    assert res.severity == "review" and "not found" in res.message


def test_r2_closed_wo(make_ctx: CtxFactory) -> None:
    res = r02_work_order.check(make_ctx(work_order_number=text("WO-1001")))
    assert res.severity == "review" and "WO-1001 is closed" in res.message


def test_r2_wo_belongs_to_another_vendor(make_ctx: CtxFactory, universe: Universe) -> None:
    res = r02_work_order.check(
        make_ctx(work_order_number=text("WO-1190"), property_or_unit=text("3A"))
    )
    assert res.severity == "review"
    assert "assigned to Oakline Electric Inc, not Rapid Rooter Plumbing LLC" in res.message
    # assigned vendor missing from the master
    tenant = _with_wo(universe, vendor_id="v-gone")
    res = r02_work_order.check(make_ctx(tenant=tenant))
    assert "assigned to another vendor" in res.message


def test_r2_unconfirmed_vendor(make_ctx: CtxFactory) -> None:
    res = r02_work_order.check(make_ctx(vendor_name=text("Rapid Rotor")))
    assert res.severity == "review" and "vendor_name" in res.fields
    assert "could not be confirmed" in res.message


def test_r2_unit_mismatch(make_ctx: CtxFactory) -> None:
    res = r02_work_order.check(make_ctx(property_or_unit=text("Unit 3A")))
    assert res.severity == "review" and res.fields == ["property_or_unit"]
    assert "Unit 12B" in res.message
    res = r02_work_order.check(make_ctx(property_or_unit=text("12B Birch Terrace")))
    assert res.severity == "review" and "Birch Terrace" in res.message
    res = r02_work_order.check(make_ctx(property_or_unit=text(None)))
    assert "does not name a property or unit" in res.message


def test_r2_unit_record_missing(make_ctx: CtxFactory, universe: Universe) -> None:
    tenant = universe(units=(Unit(id="u-3a", property_id="p-birch", label="3A"),))
    res = r02_work_order.check(make_ctx(tenant=tenant))
    assert res.severity == "review" and "not in the property list" in res.message


def test_r2_property_without_name_is_ignored(make_ctx: CtxFactory, universe: Universe) -> None:
    from invoicedesk.contracts.tenant_data import Property

    props = (*universe().properties, Property(id="p-x", name="LLC", address=""))
    res = r02_work_order.check(make_ctx(tenant=universe(properties=props)))
    assert res.passed


def test_r2_multiple_problems_combined(make_ctx: CtxFactory) -> None:
    res = r02_work_order.check(
        make_ctx(work_order_number=text("WO-1001"), property_or_unit=text("3A"))
    )
    assert res.fields == ["work_order_number", "property_or_unit"]
    assert res.message.count(";") == 1


# ---------------------------------------------------------------- R3


def test_r3_within_tolerance(make_ctx: CtxFactory) -> None:
    res = r03_three_way.check(make_ctx())
    assert res.passed
    d = res.details
    assert (d["billed_cents"], d["estimate_cents"], d["allowed_cents"]) == (
        115_000,
        120_000,
        12_000,
    )
    assert d["billed_source"] == "subtotal" and d["variance_cents"] == -5_000
    assert d["out_of_scope_lines"] == []
    assert d["estimate_lines_cents"] == 120_000


def test_r3_demo_beat_340_over_unit_12b(make_ctx: CtxFactory, universe: Universe) -> None:
    tenant = _with_wo(universe, approved_estimate_cents=81_000)  # billed $1,150.00
    res = r03_three_way.check(make_ctx(tenant=tenant))
    assert res.severity == "review" and not res.passed
    assert res.message == (
        "Rapid Rooter Plumbing invoice is $340.00 over the approved estimate ($810.00) "
        "for Unit 12B, WO-1187."
    )
    assert res.details["headline"] == "$340.00 over approved estimate for Unit 12B (WO-1187)"
    assert res.details["variance_sentence"] == res.message
    assert res.details["badge"] == "over_estimate"
    assert res.fields == ["subtotal", "total"]


def test_r3_example_from_brief_340_over_3400(make_ctx: CtxFactory, universe: Universe) -> None:
    # $3,740 billed on a $3,400 estimate is exactly at max(10 %, $100) = $340: inclusive -> pass.
    tenant = _with_wo(universe, approved_estimate_cents=340_000)
    ex = make_extraction(subtotal=money(374_000), line_items=[], tax=money(0), total=money(374_000))
    assert r03_three_way.check(make_ctx(ex, tenant=tenant)).passed
    ex = make_extraction(subtotal=money(374_001), line_items=[], tax=money(0), total=money(374_001))
    res = r03_three_way.check(make_ctx(ex, tenant=tenant))
    assert res.severity == "review"
    assert "$340.01 over the approved estimate ($3,400.00) for Unit 12B, WO-1187." in res.message


def test_r3_minimum_dollar_tolerance(make_ctx: CtxFactory, universe: Universe) -> None:
    tenant = _with_wo(universe, approved_estimate_cents=105_000)  # 10 % = $105 > $100
    assert r03_three_way.check(make_ctx(tenant=tenant)).passed  # $100 over
    tenant = _with_wo(universe, approved_estimate_cents=50_000)  # 10 % = $50 -> $100 floor
    ex = make_extraction(subtotal=money(60_000), line_items=[], tax=money(0), total=money(60_000))
    assert r03_three_way.check(make_ctx(ex, tenant=tenant)).passed
    ex = make_extraction(subtotal=money(60_001), line_items=[], tax=money(0), total=money(60_001))
    assert not r03_three_way.check(make_ctx(ex, tenant=tenant)).passed


def test_r3_under_estimate_wording(make_ctx: CtxFactory, universe: Universe) -> None:
    tenant = _with_wo(universe, approved_estimate_cents=200_000)
    res = r03_three_way.check(make_ctx(tenant=tenant))
    assert "$850.00 under the approved estimate ($2,000.00)" in res.message


def test_r3_no_wo_or_unreadable_amount(make_ctx: CtxFactory) -> None:
    res = r03_three_way.check(make_ctx(work_order_number=text(None)))
    assert res.severity == "review" and "no approved estimate" in res.message
    res = r03_three_way.check(make_ctx(work_order_number=text("WO-9999")))
    assert res.severity == "review" and "no approved estimate" in res.message
    res = r03_three_way.check(make_ctx(subtotal=money(None), total=money(None), line_items=[]))
    assert res.severity == "review" and "unreadable" in res.message


def test_billed_amount_fallbacks() -> None:
    ex = make_extraction()
    assert billed_amount(ex) == (115_000, "subtotal")
    ex = make_extraction(subtotal=money(None))
    assert billed_amount(ex) == (115_000, "line_items")
    ex2 = ex.model_copy(
        update={"line_items": [ex.line_items[0].model_copy(update={"amount": money(None)})]}
    )
    assert billed_amount(ex2) == (124_000, "total")
    ex3 = make_extraction(subtotal=money(None), line_items=[])
    assert billed_amount(ex3) == (124_000, "total")


def test_r3_total_fallback_fields(make_ctx: CtxFactory, universe: Universe) -> None:
    tenant = _with_wo(universe, approved_estimate_cents=50_000)
    res = r03_three_way.check(make_ctx(tenant=tenant, subtotal=money(None), line_items=[]))
    assert res.fields == ["total"] and res.details["billed_source"] == "total"


def test_r3_scope_lines_and_fallbacks(make_ctx: CtxFactory, universe: Universe) -> None:
    ex = make_extraction()
    odd = ex.line_items[0].model_copy(update={"description": text("Jacuzzi installation")})
    blank = ex.line_items[1].model_copy(update={"description": text(None)})
    ex = ex.model_copy(update={"line_items": [odd, blank]})
    res = r03_three_way.check(make_ctx(ex))
    assert res.details["out_of_scope_lines"] == [0, 1]

    # WO without estimate lines falls back to the scope text; unknown unit and vendor.
    tenant = _with_wo(
        universe,
        estimate_lines=(),
        unit_id="u-gone",
        vendor_id="v-gone",
        approved_estimate_cents=10_000,
    )
    res = r03_three_way.check(make_ctx(tenant=tenant, vendor_name=text("Bluewater")))
    assert res.message.startswith("Vendor invoice is ") and "for Unit ?, WO-1187" in res.message

    # Unconfirmed vendor falls back to the vendor assigned to the WO.
    tenant = _with_wo(universe, approved_estimate_cents=10_000)
    res = r03_three_way.check(make_ctx(tenant=tenant, vendor_name=text("Bluewater")))
    assert res.message.startswith("Rapid Rooter Plumbing invoice")


def test_r3_wo_number_normalized(make_ctx: CtxFactory, universe: Universe) -> None:
    w = WorkOrder(
        id="w2",
        wo_number="WO-77",
        unit_id="u-12b",
        vendor_id="v-rr",
        scope="x",
        status="open",
        approved_estimate_cents=115_000,
    )
    tenant = universe(work_orders=(w,))
    assert r03_three_way.check(make_ctx(tenant=tenant, work_order_number=text("wo #77"))).passed
