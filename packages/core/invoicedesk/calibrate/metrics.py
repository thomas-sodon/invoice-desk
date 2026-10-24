"""Calibration metrics: ECE (10 equal-width bins), reliability-diagram data, Brier score, and a
matplotlib PNG helper for the reliability diagram."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = ["ReliabilityBin", "brier_score", "ece", "plot_reliability", "reliability_bins"]


class ReliabilityBin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lo: float
    hi: float
    count: int
    mean_confidence: float | None
    accuracy: float | None


def _check(probs: Sequence[float], labels: Sequence[bool]) -> None:
    if len(probs) != len(labels):
        raise ValueError("probs and labels differ in length")
    for p in probs:
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"probability out of range: {p}")


def _bin_index(p: float, n_bins: int) -> int:
    # bins are [0, .1), [.1, .2), ..., [.9, 1.0]; p == 1.0 goes in the last bin
    return min(int(p * n_bins), n_bins - 1)


def reliability_bins(
    probs: Sequence[float], labels: Sequence[bool], *, n_bins: int = 10
) -> list[ReliabilityBin]:
    _check(probs, labels)
    sums = [0.0] * n_bins
    hits = [0] * n_bins
    counts = [0] * n_bins
    for p, y in zip(probs, labels, strict=True):
        b = _bin_index(p, n_bins)
        sums[b] += p
        hits[b] += 1 if y else 0
        counts[b] += 1
    return [
        ReliabilityBin(
            lo=b / n_bins,
            hi=(b + 1) / n_bins,
            count=counts[b],
            mean_confidence=sums[b] / counts[b] if counts[b] else None,
            accuracy=hits[b] / counts[b] if counts[b] else None,
        )
        for b in range(n_bins)
    ]


def ece(probs: Sequence[float], labels: Sequence[bool], *, n_bins: int = 10) -> float:
    """Expected calibration error: sum over bins of (n_b / N) * |accuracy_b - confidence_b|."""
    n = len(probs)
    if n == 0:
        raise ValueError("ECE of an empty set is undefined")
    total = 0.0
    for b in reliability_bins(probs, labels, n_bins=n_bins):
        if b.count and b.accuracy is not None and b.mean_confidence is not None:
            total += b.count / n * abs(b.accuracy - b.mean_confidence)
    return total


def brier_score(probs: Sequence[float], labels: Sequence[bool]) -> float:
    _check(probs, labels)
    if not probs:
        raise ValueError("Brier score of an empty set is undefined")
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in zip(probs, labels, strict=True)) / len(
        probs
    )


def plot_reliability(
    bins: Sequence[ReliabilityBin],
    path: Path,
    *,
    title: str = "Reliability (calibrated field confidence)",
    ece_value: float | None = None,
) -> Path:
    """Write a reliability-diagram PNG. Needs matplotlib (installed with the evals package)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax, axh) = plt.subplots(
        2, 1, figsize=(5, 6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    width = bins[0].hi - bins[0].lo if bins else 0.1
    centers = [(b.lo + b.hi) / 2 for b in bins]
    acc = [b.accuracy if b.accuracy is not None else 0.0 for b in bins]
    ax.plot([0, 1], [0, 1], linestyle="--", color="#888888", linewidth=1, label="perfect")
    ax.bar(centers, acc, width=width * 0.9, color="#3b6ea8", label="accuracy")
    conf = [(c, b.mean_confidence) for c, b in zip(centers, bins, strict=True) if b.count]
    if conf:
        ax.plot([c for c, _ in conf], [m for _, m in conf], "o", color="#d9822b", label="conf")
    ax.set_ylim(0, 1)
    ax.set_ylabel("accuracy")
    label = title if ece_value is None else f"{title}\nECE = {ece_value:.3f}"
    ax.set_title(label, fontsize=10)
    ax.legend(loc="upper left", fontsize=8)
    axh.bar(centers, [b.count for b in bins], width=width * 0.9, color="#9aa5b1")
    axh.set_xlabel("calibrated confidence")
    axh.set_ylabel("fields")
    axh.set_xlim(0, 1)
    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
