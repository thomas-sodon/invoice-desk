"""PR regression gate (DESIGN §5.6, CLAUDE.md "CI gates on PRs", M19).

Fails when, for the gated config (default D):

* header-field micro F1 drops by more than 1.0 point vs the ``main`` baseline;
* false-STP is above 1%;
* any of the adversarial cases is missed (and at least one case must have been scored);
* $/doc rises by more than 20% vs the baseline.

Inputs are run report JSONs (``evals/reports/*.json``) or compact baselines written by
``invoicedesk-eval baseline`` (``{"config", "split", "mode", "metrics": {...}}``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "F1_MAX_DROP",
    "FALSE_STP_MAX",
    "USD_MAX_RISE",
    "Check",
    "GateResult",
    "compact_baseline",
    "evaluate_gate",
    "load_meta",
    "load_metrics",
]

F1_MAX_DROP = 0.010  # 1.0 point
FALSE_STP_MAX = 0.01
USD_MAX_RISE = 0.20


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class GateResult:
    config: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def markdown(self) -> str:
        head = "PASSED" if self.passed else "FAILED"
        out = [f"### Eval gate (config {self.config}): {head}", "", "| Check | Result | Detail |"]
        out.append("|---|---|---|")
        for c in self.checks:
            out.append(f"| {c.name} | {'pass' if c.passed else '**FAIL**'} | {c.detail} |")
        return "\n".join(out) + "\n"


def _value(metrics: Mapping[str, Any], name: str) -> float | None:
    m = metrics.get(name)
    v = m.get("value") if isinstance(m, Mapping) else m
    return None if v is None else float(v)


def load_metrics(path: Path, config: str) -> dict[str, Any]:
    """``{metric: {"value", ...}}`` for ``config`` from a report or compact baseline."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "runs" in data:
        for r in data["runs"]:
            if r.get("config") == config:
                if r.get("skipped"):
                    raise ValueError(f"config {config} was skipped in {path}: {r['skipped']}")
                return dict(r["metrics"])
        raise KeyError(f"config {config!r} not in report {path}")
    if data.get("config") not in (None, config):
        raise KeyError(f"baseline {path} is for config {data.get('config')!r}, not {config!r}")
    return dict(data["metrics"])


def load_meta(path: Path) -> dict[str, Any]:
    """``{"mode", "split"}`` of a report or compact baseline (``None`` when not recorded)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"mode": data.get("mode"), "split": data.get("split")}


def compact_baseline(report: Mapping[str, Any], config: str) -> dict[str, Any]:
    for r in report["runs"]:
        if r.get("config") == config and not r.get("skipped"):
            return {
                "config": config,
                "split": report.get("split"),
                "mode": report.get("mode"),
                "git_sha": report.get("git_sha"),
                "label": report.get("label"),
                "metrics": {k: {"value": v.get("value")} for k, v in r["metrics"].items()},
            }
    raise KeyError(f"config {config!r} not in report")


def evaluate_gate(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    config: str = "D",
    current_meta: Mapping[str, Any] | None = None,
    baseline_meta: Mapping[str, Any] | None = None,
) -> GateResult:
    res = GateResult(config=config)
    if current_meta is not None and baseline_meta is not None:
        # An offline (oracle) baseline vs a live run, or a different split, makes the
        # relative F1 and $/doc checks meaningless; fail rather than compare them.
        diff = [
            f"{k} {current_meta.get(k)!r} vs {baseline_meta.get(k)!r}"
            for k in ("mode", "split")
            if current_meta.get(k) is not None
            and baseline_meta.get(k) is not None
            and current_meta.get(k) != baseline_meta.get(k)
        ]
        res.checks.append(
            Check(
                "baseline comparable",
                not diff,
                "; ".join(diff) if diff else "same mode and split",
            )
        )
    f1_now, f1_base = _value(current, "field_f1"), _value(baseline, "field_f1")
    if f1_now is None or f1_base is None:
        res.checks.append(Check("field F1 vs main", False, "field F1 missing"))
    else:
        drop = f1_base - f1_now
        res.checks.append(
            Check(
                "field F1 vs main",
                drop <= F1_MAX_DROP + 1e-12,
                f"{f1_now:.4f} vs {f1_base:.4f} (drop {100 * drop:+.2f} pt; max 1.00 pt)",
            )
        )
    fstp = _value(current, "false_stp")
    res.checks.append(
        Check(
            "false-STP <= 1%",
            fstp is None or fstp <= FALSE_STP_MAX + 1e-12,
            "no auto-posted documents" if fstp is None else f"{100 * fstp:.2f}%",
        )
    )
    misses = _value(current, "adversarial_misses")
    res.checks.append(
        Check(
            "zero adversarial misses",
            misses is not None and misses == 0,
            "no adversarial cases were scored" if misses is None else f"{int(misses)} missed",
        )
    )
    usd_now, usd_base = _value(current, "usd_per_doc"), _value(baseline, "usd_per_doc")
    if usd_now is None or usd_base is None:
        res.checks.append(Check("$/doc vs main", False, "$/doc missing"))
    elif usd_base == 0:
        res.checks.append(Check("$/doc vs main", usd_now == 0, f"${usd_now:.5f} vs $0 baseline"))
    else:
        rise = usd_now / usd_base - 1
        res.checks.append(
            Check(
                "$/doc vs main",
                rise <= USD_MAX_RISE + 1e-12,
                f"${usd_now:.5f} vs ${usd_base:.5f} ({100 * rise:+.1f}%; max +20%)",
            )
        )
    return res


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
