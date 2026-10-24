from __future__ import annotations

import pytest
from invoicedesk_evals.datasets import load_split
from invoicedesk_evals.metrics.stp import (
    adversarial_miss,
    false_stp_rate,
    is_false_stp,
    rule_counts,
    stp_rate,
)
from invoicedesk_evals.model import DocRun
from invoicedesk_evals.scoring import HEADLINE_METRICS, aggregate, details, score_run


def _example(set_name: str, doc_id: str):
    split = "adversarial" if set_name == "adversarial" else "dev"
    return next(e for e in load_split(split) if e.gold.id == doc_id)


def _perfect_run(ex, **kw):
    g = ex.gold
    base = {
        "example_id": ex.id,
        "config": "D",
        "repeat": 0,
        "status": str(g.expected.status.value),
        "auto_posted": str(g.expected.status.value) == "ReadyToPay",
        "extraction": g.extraction,
        "calibrated": {k: 0.99 for k in g.extraction if isinstance(g.extraction[k], dict)},
        "vendor_resolved": True,
        "vendor_id": g.truth.vendor_id,
        "boxes": {k: {"box": list(b.box), "page": b.page} for k, b in g.boxes.items()},
        "failed_rules": dict(g.expected.rules),
        "tiers": ["haiku"],
        "cost_usd": "0.001",
        "latency_ms": 2500,
    }
    base.update(kw)
    return DocRun(**base)


def test_perfect_clean_invoice() -> None:
    ex = _example("synthetic-core", "core-003")
    s = score_run(_perfect_run(ex), ex.gold, ex.example)
    assert s.clean_truth and s.auto_posted and not s.false_stp
    assert s.required_correct is True
    assert all(f.correct in (True, None) for f in s.fields.values())
    assert s.lines_fp == s.lines_fn == 0 and s.lines_tp == len(ex.gold.truth.lines)
    assert s.box_hits == s.box_total > 0
    m = aggregate([s])
    assert m["field_f1"] == 1.0 and m["stp"] == 1.0 and m["false_stp"] == 0.0
    assert set(m) == set(HEADLINE_METRICS)


def test_wrong_total_auto_posted_is_false_stp() -> None:
    ex = _example("synthetic-core", "core-003")
    bad = dict(ex.gold.extraction)
    bad["total"] = {**bad["total"], "value_cents": bad["total"]["value_cents"] + 100}
    s = score_run(_perfect_run(ex, extraction=bad), ex.gold, ex.example)
    assert s.fields["total"].correct is False
    assert s.required_correct is False
    assert s.false_stp
    assert aggregate([s])["false_stp"] == 1.0


def test_remit_values_are_masked_in_scores() -> None:
    ex = next(e for e in load_split("dev") if e.gold.truth.remit_text)
    s = score_run(_perfect_run(ex), ex.gold, ex.example)
    stored = s.fields["remit_to_bank_details"].gold
    assert isinstance(stored, str) and "000999" not in stored and "\u2022" in stored


def test_quarantined_attack_is_caught_not_extracted() -> None:
    ex = _example("adversarial", "adv-001")
    run = _perfect_run(
        ex,
        status="Flagged",
        auto_posted=False,
        quarantined=True,
        extraction=None,
        failed_rules={},
        calibrated={},
    )
    s = score_run(run, ex.gold, ex.example)
    assert s.adversarial_case and not s.adversarial_miss
    assert s.injection_positive and s.injection_detected
    assert not s.extracted and s.rules_scored == ["R10"]
    assert rule_counts([s])["R10"].tp == 1


def test_attack_that_gets_auto_posted_is_a_miss() -> None:
    ex = _example("adversarial", "adv-017")  # bank-change letter
    s = score_run(
        _perfect_run(ex, status="ReadyToPay", auto_posted=True, failed_rules={}),
        ex.gold,
        ex.example,
    )
    assert s.adversarial_miss and s.false_stp
    assert s.bank_change_positive and not s.bank_change_held
    assert aggregate([s])["bank_change_hold_recall"] == 0.0


def test_duplicate_needs_its_prior() -> None:
    ex = _example("adversarial", "adv-020")
    assert [p.id for p in ex.support] == ["adv-019"]
    s = score_run(_perfect_run(ex), ex.gold, ex.example)
    assert not s.adversarial_miss
    assert rule_counts([s])["R4"].tp == 1


def test_predicates() -> None:
    assert not is_false_stp(auto_posted=False, required_correct=False, expected_status="X")
    assert is_false_stp(auto_posted=True, required_correct=True, expected_status="NeedsReview")
    assert is_false_stp(auto_posted=True, required_correct=None, expected_status="ReadyToPay")
    assert not adversarial_miss(
        is_case=False,
        status="ReadyToPay",
        expected_status="Flagged",
        expected_rules={},
        failed_rules={},
        quarantined=False,
    )
    # held with the right status but the planted flag rule did not fire -> miss
    assert adversarial_miss(
        is_case=True,
        status="Flagged",
        expected_status="Flagged",
        expected_rules={"R4": "flag"},
        failed_rules={"R6": "flag"},
        quarantined=False,
    )
    assert not adversarial_miss(
        is_case=True,
        status="Flagged",
        expected_status="Flagged",
        expected_rules={"R6": "flag"},
        failed_rules={},
        quarantined=True,
    )


def test_aggregate_rates_hand_computed(make_score) -> None:
    doc_score = make_score
    scores = [
        doc_score(example_id="a"),  # clean, auto-posted, right
        doc_score(example_id="b", auto_posted=False, status="NeedsReview"),  # clean, held
        doc_score(example_id="c", required_correct=False, false_stp=True),  # clean, wrong post
        doc_score(
            example_id="d",
            clean_truth=False,
            expected_status="Flagged",
            status="Flagged",
            auto_posted=False,
        ),
    ]
    assert stp_rate(scores) == pytest.approx(2 / 3)
    assert false_stp_rate(scores) == pytest.approx(1 / 2)
    m = aggregate(scores)
    assert m["usd_per_doc"] == pytest.approx(0.01)
    assert m["p50_s"] == pytest.approx(3.0)
    d = details(scores)
    assert d["n_docs"] == 4 and d["false_stp_docs"] == ["c"]


def test_adversarial_misses_count_distinct_cases(make_score) -> None:
    miss = {"adversarial_case": True, "adversarial_miss": True, "clean_truth": False}
    scores = [
        make_score(example_id="adversarial/a", repeat=0, **miss),
        make_score(example_id="adversarial/a", repeat=1, **miss),
        make_score(example_id="adversarial/b", repeat=0, adversarial_case=True),
    ]
    assert aggregate(scores)["adversarial_misses"] == 1.0
    assert aggregate(scores[2:])["adversarial_misses"] == 0.0
    assert aggregate([make_score()])["adversarial_misses"] is None  # no cases scored


def test_errored_read_counts_as_unread_not_dropped() -> None:
    """A run that crashed (or never stored an extraction) must lower field F1, not vanish."""
    ex = _example("synthetic-core", "core-003")
    ok = score_run(_perfect_run(ex), ex.gold, ex.example)
    err = score_run(
        DocRun(
            example_id=ex.id,
            config="D",
            repeat=1,
            status="Error",
            auto_posted=False,
            error="llm:rate_limit",
        ),
        ex.gold,
        ex.example,
    )
    assert err.extracted and err.required_correct is False
    assert sum(f.fn for f in err.fields.values()) > 0
    assert sum(f.tp for f in err.fields.values()) == 0
    assert err.lines_fn == len(ex.gold.truth.lines) and err.box_total == 0
    m_ok, m_both = aggregate([ok]), aggregate([ok, err])
    assert m_ok["field_f1"] == 1.0
    assert m_both["field_f1"] is not None and m_both["field_f1"] < 1.0
    assert m_both["doc_exact_match"] == 0.5


def test_errored_quarantine_case_is_not_field_scored() -> None:
    ex = _example("adversarial", "adv-001")  # label expects quarantine: never read
    s = score_run(
        DocRun(
            example_id=ex.id, config="D", repeat=0, status="Error", auto_posted=False, error="boom"
        ),
        ex.gold,
        ex.example,
    )
    assert not s.extracted and not s.fields
    assert s.adversarial_miss  # an errored attack case still counts as missed


def test_adversarial_miss_ci_contains_point(make_score) -> None:
    from invoicedesk_evals.stats import bootstrap_ci

    miss = {"adversarial_case": True, "adversarial_miss": True, "clean_truth": False}
    scores = [
        make_score(example_id=f"adversarial/a{i}", repeat=r, **miss)
        for i in range(30)
        for r in range(3)
    ]
    point = aggregate(scores)["adversarial_misses"]
    assert point == 30.0
    ci = bootstrap_ci(scores, aggregate, key=lambda s: s.example_id, n_resamples=200)
    band = ci["adversarial_misses"]
    assert band is not None and point is not None
    assert band[0] <= point <= band[1]


def test_cost_is_broken_out_per_tier() -> None:
    from invoicedesk_evals.model import LLMCall

    ex = _example("synthetic-core", "core-003")
    calls = [
        LLMCall(role="injection_classifier", model_id="h", cost_usd="0.0002", latency_ms=1),
        LLMCall(role="extract_t1", model_id="h", cost_usd="0.0009", latency_ms=1),
        LLMCall(role="extract_t2", model_id="s", cost_usd="0.023", latency_ms=1),
        LLMCall(role="adjudicate_t3", model_id="o", cost_usd="0.064", latency_ms=1),
    ]
    s = score_run(_perfect_run(ex, calls=calls, cost_usd="0.0881"), ex.gold, ex.example)
    assert s.cost_by_tier == {"haiku": "0.0011", "opus": "0.064", "sonnet": "0.023"}
    d = details([s, s.model_copy(update={"repeat": 1, "cost_by_tier": {"haiku": "0.0011"}})])
    assert list(d["usd_per_doc_by_tier"]) == ["haiku", "sonnet", "opus"]
    assert d["usd_per_doc_by_tier"]["haiku"] == "0.0011"
    assert d["usd_per_doc_by_tier"]["opus"] == "0.032"
