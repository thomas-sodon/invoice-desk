from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from invoicedesk_evals.metrics import calibration, cost
from invoicedesk_evals.metrics.boxes import iou, score_boxes


def test_ece_hand_computed() -> None:
    pairs = [(0.9, True), (0.9, False), (0.1, False), (0.1, False)]
    # bin 0.9: conf .9 acc .5 -> .4 * 2/4 ; bin 0.1: conf .1 acc 0 -> .1 * 2/4
    assert calibration.ece(pairs) == pytest.approx(0.25)
    assert calibration.ece([(1.0, True), (0.0, False)]) == pytest.approx(0.0)
    assert calibration.ece([]) is None
    rel = calibration.reliability(pairs)
    assert [(b["count"], b["accuracy"]) for b in rel] == [(2, 0.0), (2, 0.5)]


def test_reliability_png(tmp_path: Path) -> None:
    p = calibration.reliability_png(
        [(0.95, True), (0.6, False), (0.8, True)], tmp_path / "r.png", title="t"
    )
    assert p is not None and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert calibration.reliability_png([], tmp_path / "x.png", title="t") is None


def test_cost_and_latency() -> None:
    assert cost.usd_per_doc(["0.01", "0.02", Decimal("0.03")]) == Decimal("0.02")
    assert cost.usd_per_doc([]) is None
    assert cost.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert cost.percentile([], 95) is None
    agg = cost.cost_from_extractions(
        [
            {"tier": "haiku", "cost_usd": "0.0009", "latency_ms": 1100, "input_tokens": 10},
            {"tier": "sonnet", "cost_usd": "0.023", "latency_ms": 4000},
            {"tier": "haiku", "cost_usd": "0.0009", "latency_ms": 900},
        ]
    )
    assert agg["calls"] == 3
    assert agg["total_usd"] == Decimal("0.0248")
    assert agg["by_tier"]["haiku"]["latency_ms"] == 2000
    rates = cost.escalation_rates([["haiku"], ["haiku", "sonnet"], ["haiku", "sonnet", "opus"], []])
    assert rates == {"sonnet": pytest.approx(2 / 3), "opus": pytest.approx(1 / 3)}


def test_iou_and_box_hits() -> None:
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)
    assert iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0
    gold = {
        "total": {"box": [100, 100, 200, 120], "page": 0},
        "invoice_number": {"box": [10, 10, 60, 20], "page": 0},
        "invoice_date": {"box": [10, 30, 60, 40], "page": 0},
    }
    pred = {
        "total": {"box": [102, 101, 199, 121], "page": 0},  # hit
        "invoice_number": {"box": [10, 10, 60, 20], "page": 1},  # wrong page
    }  # invoice_date missing -> miss
    s = score_boxes(gold, pred, medium="native")
    assert (s.hits, s.total) == (1, 3)
    assert score_boxes(gold, pred, medium="photo").total == 0  # photos are not box-scored
    assert score_boxes(gold, pred, medium="native", box_eval=False).total == 0
