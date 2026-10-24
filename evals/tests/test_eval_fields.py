from __future__ import annotations

import pytest
from invoicedesk.testing.builders import make_extraction, money, text
from invoicedesk_evals.metrics.fields import Counts, micro, score_header_fields, score_value
from invoicedesk_evals.normalize import (
    norm_date,
    norm_text,
    norm_work_order,
    normalize_field,
)


def test_score_value_null_vs_value_table() -> None:
    assert score_value("a", "a") == Counts(tp=1)
    assert score_value("a", "b") == Counts(fp=1, fn=1)
    assert score_value("a", None) == Counts(fn=1)
    assert score_value(None, "b") == Counts(fp=1)
    assert score_value(None, None) == Counts()


def test_counts_prf_hand_computed() -> None:
    c = Counts(tp=6, fp=2, fn=4)
    assert c.precision == pytest.approx(6 / 8)
    assert c.recall == pytest.approx(6 / 10)
    assert c.f1 == pytest.approx(2 * 6 / (2 * 6 + 2 + 4))
    assert Counts().f1 is None


def test_normalizers() -> None:
    assert norm_work_order("WO #1187") == norm_work_order("wo-1187") == "1187"
    assert norm_work_order("Work Order No. 1187") == "1187"
    assert norm_work_order("WO-1188") != norm_work_order("WO-1187")
    assert norm_text("  Unit 12B,  Juniper House. ") == "unit 12b juniper house"
    assert norm_date("2026-10-10") == "2026-10-10"
    assert norm_date("10/10/2026") is None  # never guesses
    assert normalize_field("total", 124000) == 124000
    assert normalize_field("vendor_tax_id_last4", "00-0006102") == "6102"
    with pytest.raises(TypeError):
        normalize_field("total", 1240.0)  # money is integer cents, never float


def test_header_fields_hand_computed_f1() -> None:
    gold = make_extraction()
    pred = make_extraction(
        total=money(124100),  # wrong -> FP + FN
        invoice_number=text(None),  # missing -> FN
        remit_to_bank_details=text("Acct 000123456789"),  # gold has none -> FP
    )
    out = score_header_fields(gold, pred)
    assert out["total"].counts == Counts(fp=1, fn=1)
    assert out["invoice_number"].counts == Counts(fn=1)
    assert out["remit_to_bank_details"].counts == Counts(fp=1)
    assert out["vendor_name"].correct is True
    c = micro([out])
    n_gold = sum(1 for o in out.values() if o.gold is not None)
    # every other gold field is a TP
    assert c.tp == n_gold - 2
    assert c.fp == 2
    assert c.fn == 2
    assert c.f1 == pytest.approx(2 * c.tp / (2 * c.tp + 4))


def test_vendor_scored_by_resolved_vendor_id() -> None:
    gold = make_extraction()
    pred = make_extraction(vendor_name=text("RAPID ROOTER PLUMBING"))
    right = score_header_fields(
        gold, pred, gold_vendor_id="v1", pred_vendor_id="v1", vendor_resolved=True
    )
    assert right["vendor_name"].correct is True
    # the lookalike resolves to nothing: wrong even though the printed name is close
    wrong = score_header_fields(
        gold, pred, gold_vendor_id="v1", pred_vendor_id=None, vendor_resolved=True
    )
    assert wrong["vendor_name"].counts == Counts(fp=1, fn=1)
    # without a resolution step (naive baseline), the printed name is compared
    by_name = score_header_fields(gold, make_extraction(), gold_vendor_id="v1")
    assert by_name["vendor_name"].correct is True


def test_excluded_and_missing_prediction() -> None:
    gold = make_extraction()
    out = score_header_fields(gold, None, exclude=["total"])
    assert "total" not in out
    assert all(o.counts.tp == 0 for o in out.values())
