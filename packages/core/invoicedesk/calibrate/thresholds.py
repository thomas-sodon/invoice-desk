"""Choose ``tau_field`` and ``tau_hi`` on the dev split (DESIGN §5.5, §5.6).

A document passes the confidence gate when every required field's calibrated probability is at
or above ``tau_field`` (R9). False-STP is the share of passing documents with any wrong required
field (documents held by other rules are excluded from the passing set, since they are never
auto-posted). We pick the *smallest* ``tau_field`` (most STP) such that false-STP stays at or
below the target for that threshold **and every higher grid threshold** (a robust choice: a
noisy low threshold that passes by luck is not taken).

``tau_hi`` is the Opus-escalation threshold: after Sonnet, a document whose minimum required
confidence is below ``tau_hi`` goes to Opus. It is the smallest grid value >= ``tau_field``
whose passing set meets the stricter ``hi_target`` (default: half the false-STP target).
If no grid value meets a target, the threshold is 1.0 (fail safe: hold everything).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.calibrate.combiner import assert_dev_split

__all__ = ["DocScores", "ThresholdChoice", "choose_thresholds", "gate_stats"]


class DocScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    required_probs: dict[str, float]  # calibrated probability per required field
    required_correct: dict[str, bool]  # gold check per required field
    held_by_other_rules: bool = False  # e.g. R6/R7/R4 would hold it anyway


class ThresholdChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tau_field: float
    tau_hi: float
    stp_rate: float = Field(description="share of all dev docs passing the gate at tau_field")
    false_stp_rate: float
    n_docs: int
    n_auto: int
    max_false_stp: float
    hi_target: float


def _min_prob(d: DocScores) -> float:
    return min(d.required_probs.values()) if d.required_probs else 0.0


def gate_stats(docs: Sequence[DocScores], tau: float) -> tuple[int, int]:
    """``(n_auto, n_false)`` at threshold ``tau``."""
    n_auto = n_false = 0
    for d in docs:
        if d.held_by_other_rules or _min_prob(d) < tau:
            continue
        n_auto += 1
        if not all(d.required_correct.get(k, False) for k in d.required_probs):
            n_false += 1
    return n_auto, n_false


def _rate(n_false: int, n_auto: int) -> float:
    return n_false / n_auto if n_auto else 0.0


def _smallest_robust(docs: Sequence[DocScores], grid: Sequence[float], target: float) -> float:
    ok_from: float | None = None
    for tau in sorted(grid, reverse=True):
        n_auto, n_false = gate_stats(docs, tau)
        if _rate(n_false, n_auto) <= target:
            ok_from = tau
        else:
            break
    return 1.0 if ok_from is None else ok_from


def choose_thresholds(
    docs: Sequence[DocScores],
    *,
    split: str,
    max_false_stp: float = 0.01,
    hi_target: float | None = None,
    grid: Sequence[float] | None = None,
) -> ThresholdChoice:
    assert_dev_split(split)
    if not docs:
        raise ValueError("no documents")
    grid = sorted(set(grid or [round(0.50 + 0.005 * i, 3) for i in range(101)]))
    hi = max_false_stp / 2 if hi_target is None else hi_target
    tau_field = _smallest_robust(docs, grid, max_false_stp)
    tau_hi = max(tau_field, _smallest_robust(docs, [g for g in grid if g >= tau_field], hi))
    n_auto, n_false = gate_stats(docs, tau_field)
    return ThresholdChoice(
        tau_field=tau_field,
        tau_hi=tau_hi,
        stp_rate=n_auto / len(docs),
        false_stp_rate=_rate(n_false, n_auto),
        n_docs=len(docs),
        n_auto=n_auto,
        max_false_stp=max_false_stp,
        hi_target=hi,
    )
