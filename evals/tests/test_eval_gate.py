from __future__ import annotations

import json
from pathlib import Path

import pytest
from invoicedesk_evals.gate import compact_baseline, evaluate_gate, load_metrics
from invoicedesk_evals.runner import main


def _m(**kw: float | None) -> dict[str, dict[str, float | None]]:
    base: dict[str, float | None] = {
        "field_f1": 0.97,
        "false_stp": 0.0,
        "adversarial_misses": 0.0,
        "usd_per_doc": 0.011,
    }
    base.update(kw)
    return {k: {"value": v} for k, v in base.items()}


def test_gate_passes_on_equal_runs() -> None:
    res = evaluate_gate(_m(), _m())
    assert res.passed
    assert "PASSED" in res.markdown()


@pytest.mark.parametrize(
    ("current", "failing"),
    [
        ({"field_f1": 0.959}, "field F1 vs main"),  # 1.1 pt drop
        ({"false_stp": 0.011}, "false-STP <= 1%"),
        ({"adversarial_misses": 1.0}, "zero adversarial misses"),
        ({"adversarial_misses": None}, "zero adversarial misses"),  # none scored
        ({"usd_per_doc": 0.0133}, "$/doc vs main"),  # +21%
    ],
)
def test_gate_fails(current: dict[str, float | None], failing: str) -> None:
    res = evaluate_gate(_m(**current), _m())
    assert not res.passed
    assert [c.name for c in res.checks if not c.passed] == [failing]


def test_gate_boundaries_pass() -> None:
    assert evaluate_gate(_m(field_f1=0.96, usd_per_doc=0.0132, false_stp=0.01), _m()).passed


def _report(path: Path, metrics: dict[str, dict[str, float | None]]) -> Path:
    path.write_text(
        json.dumps(
            {"split": "smoke", "mode": "offline", "runs": [{"config": "D", "metrics": metrics}]}
        )
    )
    return path


def test_gate_cli_fails_on_deliberately_broken_baseline(tmp_path: Path) -> None:
    current = _report(tmp_path / "current.json", _m())
    good = tmp_path / "baseline.json"
    good.write_text(json.dumps(compact_baseline(json.loads(current.read_text()), "D")))
    assert main(["gate", "--current", str(current), "--baseline", str(good)]) == 0
    # A baseline from a "main" that was 3 points better and 50% cheaper: this run regresses.
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"config": "D", "metrics": _m(field_f1=1.0, usd_per_doc=0.005)}))
    assert main(["gate", "--current", str(current), "--baseline", str(broken)]) == 1
    # A baseline for another config is refused rather than silently compared.
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"config": "A", "metrics": _m()}))
    with pytest.raises(KeyError):
        load_metrics(other, "D")
    # Missing baseline: hard error unless explicitly allowed (first PR on a new main).
    assert main(["gate", "--current", str(current), "--baseline", str(tmp_path / "nope")]) == 2
    assert (
        main(
            [
                "gate",
                "--current",
                str(current),
                "--baseline",
                str(tmp_path / "nope"),
                "--baseline-missing-ok",
            ]
        )
        == 0
    )


def test_gate_cli_writes_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    current = _report(tmp_path / "c.json", _m(adversarial_misses=2.0))
    assert main(["gate", "--current", str(current), "--baseline", str(current)]) == 1
    assert "FAIL" in summary.read_text()


def test_baseline_cli(tmp_path: Path) -> None:
    rep = _report(tmp_path / "r.json", _m())
    out = tmp_path / "b" / "smoke.json"
    assert main(["baseline", "--report", str(rep), "--config", "D", "--out", str(out)]) == 0
    assert load_metrics(out, "D")["field_f1"]["value"] == 0.97
