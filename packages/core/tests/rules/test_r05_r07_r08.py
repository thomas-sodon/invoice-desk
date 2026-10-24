from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

from invoicedesk.config.tenant import DateConfig, RulesConfig
from invoicedesk.contracts.rules import DocumentInfo, RuleContext
from invoicedesk.contracts.tenant_data import ApprovalRecord
from invoicedesk.rules import r05_arithmetic, r07_amount_cap, r08_dates
from invoicedesk.rules.r05_arithmetic import parse_quantity
from invoicedesk.testing.builders import date, make_extraction, money, text

CtxFactory = Callable[..., RuleContext]

# ---------------------------------------------------------------- R5


def test_r5_clean(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx())
    assert res.passed
    assert res.details == {"line_sum_cents": 115_000, "tax_rate_bps": 783}


def test_r5_lines_vs_subtotal(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx(subtotal=money(116_000), total=money(125_000)))
    assert res.severity == "review" and res.fields == ["subtotal"]
    assert "line items add up to $1,150.00, but the subtotal says $1,160.00" in res.message


def test_r5_subtotal_plus_tax_vs_total(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx(total=money(134_000)))
    assert res.fields == ["total"]
    assert (
        "subtotal $1,150.00 + tax $90.00 = $1,240.00, but the total says $1,340.00" in res.message
    )


def test_r5_rounding_slack(make_ctx: CtxFactory) -> None:
    assert r05_arithmetic.check(make_ctx(total=money(124_001))).passed
    assert not r05_arithmetic.check(make_ctx(total=money(124_002))).passed


def test_r5_no_subtotal_uses_line_sum(make_ctx: CtxFactory) -> None:
    assert r05_arithmetic.check(make_ctx(subtotal=money(None))).passed
    res = r05_arithmetic.check(make_ctx(subtotal=money(None), total=money(130_000)))
    assert "line items $1,150.00 + tax" in res.message


def test_r5_missing_tax_means_zero(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx(tax=money(None), total=money(115_000)))
    assert res.passed and res.details["tax_rate_bps"] == 0


def test_r5_tax_rate_out_of_range(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx(tax=money(20_000), total=money(135_000)))
    assert res.fields == ["tax"]
    assert "tax rate 17.39% is outside the expected 0.00%-12.00% range" in res.message
    res = r05_arithmetic.check(make_ctx(tax=money(-1_000), total=money(114_000)))
    assert res.fields == ["tax"]  # negative tax on a positive base


def test_r5_unreadable_total(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(make_ctx(total=money(None, 0.1)))
    assert res.severity == "review" and res.fields == ["total"]
    assert "total is unreadable" in res.message


def test_r5_line_qty_times_price(make_ctx: CtxFactory) -> None:
    ex = make_extraction()
    bad = ex.line_items[1].model_copy(update={"quantity": text("3 hrs")})
    res = r05_arithmetic.check(
        make_ctx(ex.model_copy(update={"line_items": [ex.line_items[0], bad]}))
    )
    assert res.fields == ["line_items[1].amount"]
    assert "line 2: 3 hrs x $150.00 is $450.00, not $300.00" in res.message
    # unparsable quantity or missing prices are skipped, fractional quantities work
    for qty in ("ea", None):
        li = ex.line_items[1].model_copy(update={"quantity": text(qty)})
        assert r05_arithmetic.check(
            make_ctx(ex.model_copy(update={"line_items": [ex.line_items[0], li]}))
        ).passed
    li = ex.line_items[1].model_copy(update={"quantity": text("1.5"), "unit_price": money(20_000)})
    assert r05_arithmetic.check(
        make_ctx(ex.model_copy(update={"line_items": [ex.line_items[0], li]}))
    ).passed
    li = ex.line_items[1].model_copy(update={"unit_price": money(None)})
    assert r05_arithmetic.check(
        make_ctx(ex.model_copy(update={"line_items": [ex.line_items[0], li]}))
    ).passed


def test_r5_missing_line_amount_and_no_lines(make_ctx: CtxFactory) -> None:
    ex = make_extraction()
    li = ex.line_items[1].model_copy(update={"amount": money(None)})
    res = r05_arithmetic.check(
        make_ctx(ex.model_copy(update={"line_items": [ex.line_items[0], li]}))
    )
    assert res.passed and res.details["line_sum_cents"] is None
    res = r05_arithmetic.check(make_ctx(line_items=[], subtotal=money(None), tax=money(0)))
    assert res.passed and res.details["tax_rate_bps"] is None  # no base to check


def test_r5_zero_base(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(
        make_ctx(line_items=[], subtotal=money(0), tax=money(0), total=money(0))
    )
    assert res.passed and res.details["tax_rate_bps"] is None


def test_r5_credit_memo_negative_amounts(make_ctx: CtxFactory) -> None:
    res = r05_arithmetic.check(
        make_ctx(
            doc_type="credit_memo",
            line_items=[],
            subtotal=money(-10_000),
            tax=money(-800),
            total=money(-10_800),
        )
    )
    assert res.passed and res.details["tax_rate_bps"] == 800


def test_parse_quantity() -> None:
    assert parse_quantity("2") == Decimal(2)
    assert parse_quantity(" 1.25 hr") == Decimal("1.25")
    assert parse_quantity("ea") is None
    assert parse_quantity(None) is None
    assert parse_quantity("") is None


# ---------------------------------------------------------------- R7


def test_r7_under_and_at_cap(make_ctx: CtxFactory) -> None:
    assert r07_amount_cap.check(make_ctx()).passed
    assert r07_amount_cap.check(make_ctx(total=money(500_000))).passed


def test_r7_over_cap(make_ctx: CtxFactory) -> None:
    res = r07_amount_cap.check(make_ctx(total=money(500_001)))
    assert res.severity == "review" and res.fields == ["total"]
    assert res.message == "Total $5,000.01 is over the $5,000.00 approval limit."
    assert res.details["badge"] == "over_cap"


def test_r7_over_cap_regardless_of_confidence(make_ctx: CtxFactory) -> None:
    res = r07_amount_cap.check(make_ctx(total=money(900_000, conf=1.0), conf={"total": 1.0}))
    assert res.severity == "review"


def test_r7_approval_by_non_uploader(make_ctx: CtxFactory) -> None:
    ok = ApprovalRecord(approver_id="u-boss", kind="over_cap", decision="approve")
    res = r07_amount_cap.check(make_ctx(total=money(900_000), approvals=(ok,)))
    assert res.passed and res.details["approved"] is True


def test_r7_uploader_or_rejected_approval_does_not_clear(make_ctx: CtxFactory) -> None:
    mine = ApprovalRecord(approver_id="u-up", kind="over_cap", decision="approve")
    ok = ApprovalRecord(approver_id="u-boss", kind="over_cap", decision="approve")
    rej = ApprovalRecord(approver_id="u-cfo", kind="over_cap", decision="reject")
    for approvals in ((mine,), (ok, rej)):
        res = r07_amount_cap.check(make_ctx(total=money(900_000), approvals=approvals))
        assert res.severity == "review"
    doc = DocumentInfo(document_id="d", sha256="s", uploaded_by="u-boss")
    res = r07_amount_cap.check(make_ctx(total=money(900_000), approvals=(ok,), document=doc))
    assert res.severity == "review"


def test_r7_unreadable_total(make_ctx: CtxFactory) -> None:
    res = r07_amount_cap.check(make_ctx(total=money(None)))
    assert res.severity == "review" and "cannot be checked" in res.message


def test_r7_cap_from_config(make_ctx: CtxFactory, config: RulesConfig) -> None:
    cfg = config.model_copy(update={"amount_cap_cents": 100_000})
    assert r07_amount_cap.check(make_ctx(cfg=cfg)).severity == "review"


# ---------------------------------------------------------------- R8


def test_r8_clean(make_ctx: CtxFactory) -> None:
    res = r08_dates.check(make_ctx())
    assert res.passed and res.details["today"] == "2026-10-24"


def test_r8_future(make_ctx: CtxFactory, config: RulesConfig) -> None:
    res = r08_dates.check(make_ctx(invoice_date=date("2026-10-25"), due_date=date(None)))
    assert res.severity == "review" and "in the future" in res.message
    assert r08_dates.check(make_ctx(invoice_date=date("2026-10-24"), due_date=date(None))).passed
    cfg = config.model_copy(update={"dates": DateConfig(allow_future_days=1)})
    res = r08_dates.check(make_ctx(invoice_date=date("2026-10-25"), due_date=date(None), cfg=cfg))
    assert res.passed


def test_r8_too_old(make_ctx: CtxFactory) -> None:
    edge = (dt.date(2026, 10, 24) - dt.timedelta(days=90)).isoformat()
    assert r08_dates.check(make_ctx(invoice_date=date(edge))).passed
    old = (dt.date(2026, 10, 24) - dt.timedelta(days=91)).isoformat()
    res = r08_dates.check(make_ctx(invoice_date=date(old)))
    assert "is 91 days old (limit 90)" in res.message and res.fields == ["invoice_date"]


def test_r8_missing_or_unreadable_invoice_date(make_ctx: CtxFactory) -> None:
    res = r08_dates.check(make_ctx(invoice_date=date(None)))
    assert "invoice date is missing" in res.message
    res = r08_dates.check(make_ctx(invoice_date=date("10/10/2026")))
    assert "invoice date is unreadable" in res.message


def test_r8_due_date(make_ctx: CtxFactory) -> None:
    res = r08_dates.check(make_ctx(due_date=date("2026-10-01")))
    assert res.fields == ["due_date"] and "before the invoice date" in res.message
    res = r08_dates.check(make_ctx(due_date=date("Net 30")))
    assert res.fields == ["due_date"] and "due date is unreadable" in res.message
    assert r08_dates.check(make_ctx(due_date=date("2026-10-10"))).passed  # same day is fine
    assert r08_dates.check(make_ctx(due_date=date(None))).passed
    # with no invoice date, a valid due date is not compared
    res = r08_dates.check(make_ctx(invoice_date=date(None), due_date=date("2026-01-01")))
    assert res.fields == ["invoice_date"]


def test_r8_today_is_injected(make_ctx: CtxFactory) -> None:
    res = r08_dates.check(make_ctx(today=dt.date(2027, 6, 1)))
    assert res.severity == "review" and "days old" in res.message
