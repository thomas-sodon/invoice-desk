"""Calibration: signals, heuristic, fitted combiners, ECE/reliability, threshold choice."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from invoicedesk.calibrate import (
    CALIBRATOR_ENV,
    DocScores,
    FittedCalibrator,
    HeuristicCalibrator,
    brier_score,
    build_field_signals,
    choose_thresholds,
    ece,
    feature_vector,
    fit_calibrator,
    load_calibrator,
    ocr_agreement,
    plot_reliability,
    reliability_bins,
)
from invoicedesk.contracts.calibration import Calibrator, FieldSignals
from invoicedesk.contracts.preprocess import OcrWord
from invoicedesk.testing.builders import date, make_extraction, money, text


# ---------------------------------------------------------------- features
def test_ocr_agreement_money_date_text() -> None:
    assert ocr_agreement(money(124_000), ["$1,240.00"]) == 1.0
    assert ocr_agreement(money(124_000), ["$1,240"]) == 1.0
    assert ocr_agreement(money(124_000), ["$1,290.00"]) < 1.0
    assert ocr_agreement(money(None), ["$1"]) == 0.0
    assert ocr_agreement(money(5), ["Total"]) == 0.0
    assert ocr_agreement(date("2026-10-10"), ["10/10/2026"]) == 1.0
    assert ocr_agreement(date("2026-10-10"), ["Oct", "10,", "2026"]) == 1.0
    assert ocr_agreement(date("2026-09-08"), ["9/8/2026"]) == 1.0
    assert ocr_agreement(date("2026-10-10"), ["11/12/2025"]) < 0.8
    assert ocr_agreement(date(None), ["x"]) == 0.0
    assert ocr_agreement(date("garbage"), ["garbage"]) == 1.0
    assert ocr_agreement(text("INV-2231"), ["INV-2231"]) == 1.0
    assert ocr_agreement(text("INV-2231"), ["Invoice", "#:", "INV-2231"]) > 0.7
    assert ocr_agreement(text("INV-2231"), ["INV-2281"]) < 1.0
    assert ocr_agreement(text("Rapid Rooter"), []) == 0.0
    assert ocr_agreement(text(None), ["x"]) == 0.0
    assert ocr_agreement(text("--"), ["x"]) == 0.0


def test_build_field_signals() -> None:
    words = {
        0: OcrWord(id=0, page=0, text="INV-2231", box=(0, 0, 10, 10), conf=0.9),
        1: OcrWord(id=1, page=0, text="$1,240.00", box=(0, 0, 10, 10), conf=0.9),
    }
    ex = make_extraction(
        invoice_number=text("INV-2231", 0.9, [0]),
        total=money(124_000, 0.8, [1, 99]),  # 99 is not a real word id
        handwriting_present=True,
    )
    sig = {
        s.field: s
        for s in build_field_signals(
            ex, words, arithmetic={"total": False}, agreement={"total": True, "line_items": False}
        )
    }
    assert sig["invoice_number"].ocr_agreement == 1.0
    assert sig["invoice_number"].has_source_words
    assert sig["invoice_number"].cross_tier_agree is None
    assert sig["total"].arithmetic_ok is False
    assert sig["total"].cross_tier_agree is True
    assert sig["total"].has_source_words
    assert sig["vendor_name"].has_source_words is False
    assert sig["line_items[0].amount"].cross_tier_agree is False
    assert all(s.handwriting for s in sig.values())
    assert len(feature_vector(sig["total"])) == 8


# ---------------------------------------------------------------- heuristic
def _sig(**kw: object) -> FieldSignals:
    base: dict[str, object] = {"field": "f", "raw_conf": 0.9, "ocr_agreement": 1.0}
    base.update(kw)
    return FieldSignals.model_validate(base)


def test_heuristic_is_monotone_and_penalizes() -> None:
    h = HeuristicCalibrator()
    assert isinstance(h, Calibrator)
    grid = [i / 10 for i in range(11)]
    for other in grid:
        raw_scores = [h.score(_sig(raw_conf=r, ocr_agreement=other)) for r in grid]
        assert raw_scores == sorted(raw_scores)
        ocr_scores = [h.score(_sig(raw_conf=other, ocr_agreement=o)) for o in grid]
        assert ocr_scores == sorted(ocr_scores)
    clean = h.score(_sig())
    assert clean == pytest.approx(0.9)
    assert h.score(_sig(arithmetic_ok=False)) < clean
    assert h.score(_sig(cross_tier_agree=False)) < clean
    assert h.score(_sig(cross_tier_agree=True)) > clean
    assert h.score(_sig(has_source_words=False)) < clean
    assert h.score(_sig(handwriting=True)) < clean
    assert h.calibrate([_sig(field="a"), _sig(field="b", raw_conf=0.1)]).keys() == {"a", "b"}


# ---------------------------------------------------------------- synthetic data
def _synthetic(n: int, seed: int) -> tuple[list[FieldSignals], list[bool]]:
    """Fields whose true correctness depends on OCR agreement and arithmetic; the raw
    self-report is overconfident (always 0.9-1.0), like real LLM self-reports."""
    rng = random.Random(seed)
    signals: list[FieldSignals] = []
    labels: list[bool] = []
    for i in range(n):
        ocr = rng.random()
        arith = rng.choice([True, False, None])
        p_true = 0.15 + 0.8 * ocr * (0.4 if arith is False else 1.0)
        signals.append(
            FieldSignals(
                field=f"f{i}",
                raw_conf=0.9 + 0.1 * rng.random(),
                ocr_agreement=ocr,
                arithmetic_ok=arith,
                cross_tier_agree=rng.choice([True, None]),
                has_source_words=ocr > 0.05,
            )
        )
        labels.append(rng.random() < p_true)
    return signals, labels


@pytest.mark.parametrize("method", ["logistic", "logistic_isotonic", "isotonic"])
def test_fit_improves_calibration_on_held_out(method: str) -> None:
    train_x, train_y = _synthetic(3000, seed=1)
    test_x, test_y = _synthetic(3000, seed=2)
    cal = fit_calibrator(train_x, train_y, split="dev", method=method)  # type: ignore[arg-type]
    raw_ece = ece([s.raw_conf for s in test_x], test_y)
    probs = list(cal.calibrate(test_x).values())
    fitted_ece = ece(probs, test_y)
    assert raw_ece > 0.2  # overconfident self-report
    assert fitted_ece < 0.05  # ECE target from DESIGN §5.6
    assert brier_score(probs, test_y) < brier_score([s.raw_conf for s in test_x], test_y)
    assert cal.version.startswith(method)


def test_fit_guards() -> None:
    x, y = _synthetic(100, seed=3)
    with pytest.raises(ValueError, match="dev"):
        fit_calibrator(x, y, split="test")
    with pytest.raises(ValueError, match="length"):
        fit_calibrator(x, y[:-1], split="dev")
    with pytest.raises(ValueError, match="at least"):
        fit_calibrator(x[:5], y[:5], split="dev")
    with pytest.raises(ValueError, match="one class"):
        fit_calibrator(x, [True] * len(x), split="dev")


def test_save_load_round_trip_and_load_calibrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x, y = _synthetic(500, seed=4)
    cal = fit_calibrator(x, y, split="dev", method="logistic_isotonic")
    path = cal.save(tmp_path / "cal" / "calibrator.json")
    loaded = load_calibrator(path)
    assert isinstance(loaded, FittedCalibrator)
    assert loaded.version == cal.version
    assert loaded.calibrate(x[:20]) == cal.calibrate(x[:20])
    assert json.loads(path.read_text())["kind"] == "invoicedesk.calibrator"

    monkeypatch.delenv(CALIBRATOR_ENV, raising=False)
    assert load_calibrator().version == "heuristic-v1"
    assert load_calibrator(tmp_path / "missing.json").version == "heuristic-v1"
    monkeypatch.setenv(CALIBRATOR_ENV, str(path))
    assert load_calibrator().version == cal.version
    assert FittedCalibrator(method="logistic", coef=[0.0] * 8).predict([]).size == 0


def test_artifact_validation() -> None:
    with pytest.raises(ValueError, match="not an invoicedesk"):
        FittedCalibrator.from_dict({"kind": "other"})
    with pytest.raises(ValueError, match="feature names"):
        FittedCalibrator(method="logistic", coef=[0.0], feature_names=["x"])
    with pytest.raises(ValueError, match="coefficients"):
        FittedCalibrator(method="logistic")
    with pytest.raises(ValueError, match="isotonic"):
        FittedCalibrator(method="isotonic")


# ---------------------------------------------------------------- metrics
def test_ece_hand_computed() -> None:
    # bin [0.2,0.3): conf .25 x2, 1 hit -> |0.5-0.25| ; bin [0.9,1.0]: conf .95,1.0, 2 hits
    probs = [0.25, 0.25, 0.95, 1.0]
    labels = [True, False, True, True]
    expected = 2 / 4 * abs(0.5 - 0.25) + 2 / 4 * abs(1.0 - 0.975)
    assert ece(probs, labels) == pytest.approx(expected)
    assert ece([1.0, 0.0], [True, False]) == 0.0
    with pytest.raises(ValueError):
        ece([], [])
    with pytest.raises(ValueError):
        ece([1.2], [True])
    with pytest.raises(ValueError):
        ece([0.5], [True, False])


def test_reliability_bins_and_brier() -> None:
    bins = reliability_bins([0.05, 0.15, 0.15, 1.0], [False, True, False, True])
    assert len(bins) == 10
    assert bins[0].count == 1
    assert bins[1].count == 2
    assert bins[1].accuracy == 0.5
    assert bins[1].mean_confidence == pytest.approx(0.15)
    assert bins[9].count == 1  # p == 1.0 lands in the last bin
    assert bins[5].count == 0
    assert bins[5].accuracy is None
    assert brier_score([1.0, 0.0], [True, False]) == 0.0
    with pytest.raises(ValueError):
        brier_score([], [])


def test_plot_reliability_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    probs = [i / 100 for i in range(100)]
    labels = [i % 3 != 0 for i in range(100)]
    out = plot_reliability(
        reliability_bins(probs, labels), tmp_path / "r.png", ece_value=ece(probs, labels)
    )
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- thresholds
def _doc(i: int, p: float, correct: bool, held: bool = False) -> DocScores:
    return DocScores(
        doc_id=f"d{i}",
        required_probs={"total": p, "invoice_number": 0.99},
        required_correct={"total": correct, "invoice_number": True},
        held_by_other_rules=held,
    )


def test_choose_thresholds_keeps_false_stp_under_target() -> None:
    docs = (
        [_doc(i, 0.99, True) for i in range(150)]
        + [_doc(1000 + i, 0.97, i % 25 != 0) for i in range(100)]  # 4% wrong at .97
        + [_doc(2000 + i, 0.85, i % 4 != 0) for i in range(50)]  # 25% wrong at .85
        + [_doc(3000, 0.995, False, held=True)]  # held by another rule: not auto-posted
    )
    choice = choose_thresholds(docs, split="dev", max_false_stp=0.01)
    assert choice.false_stp_rate <= 0.01
    assert 0.97 < choice.tau_field <= 0.99
    assert choice.tau_hi >= choice.tau_field
    assert choice.n_auto == 150
    assert choice.stp_rate == pytest.approx(150 / len(docs))

    loose = choose_thresholds(docs, split="dev", max_false_stp=0.05, hi_target=0.01)
    assert loose.tau_field <= 0.97
    assert loose.false_stp_rate <= 0.05
    assert loose.tau_hi > 0.97  # the stricter hi target pushes the Opus threshold up


def test_choose_thresholds_fail_safe_and_guards() -> None:
    bad = [_doc(i, 1.0, False) for i in range(10)]
    choice = choose_thresholds(bad, split="dev", grid=[0.5, 0.9, 1.0])
    assert choice.tau_field == 1.0
    assert choice.tau_hi == 1.0
    with pytest.raises(ValueError, match="dev"):
        choose_thresholds(bad, split="test")
    with pytest.raises(ValueError):
        choose_thresholds([], split="dev")
    perfect = [_doc(i, 0.6, True) for i in range(10)]
    assert choose_thresholds(perfect, split="dev").tau_field == 0.5
    empty = DocScores(doc_id="e", required_probs={}, required_correct={})
    assert choose_thresholds([empty], split="dev").n_auto == 0  # no probabilities: fail closed
