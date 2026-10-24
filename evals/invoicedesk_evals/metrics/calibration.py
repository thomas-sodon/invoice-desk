"""Calibration (M6): ECE over 10 equal-width bins and the reliability diagram.

Pairs are ``(calibrated confidence, field exactly right)`` for every scored header field the run
returned a value for. The binning and ECE come from :mod:`invoicedesk.calibrate.metrics`, the
same code the calibrator is fitted against, so the published number and the fit agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from invoicedesk.calibrate.metrics import ece as _ece
from invoicedesk.calibrate.metrics import plot_reliability, reliability_bins

__all__ = ["N_BINS", "ece", "reliability", "reliability_png"]

N_BINS = 10

Pair = tuple[float, bool]


def _split(pairs: Sequence[Pair]) -> tuple[list[float], list[bool]]:
    probs = [min(1.0, max(0.0, float(p))) for p, _ in pairs]
    return probs, [bool(c) for _, c in pairs]


def ece(pairs: Sequence[Pair], *, n_bins: int = N_BINS) -> float | None:
    if not pairs:
        return None
    probs, labels = _split(pairs)
    return _ece(probs, labels, n_bins=n_bins)


def reliability(pairs: Sequence[Pair], *, n_bins: int = N_BINS) -> list[dict[str, float | int]]:
    """Non-empty bins as ``{"confidence", "accuracy", "count", "lo", "hi"}`` (the scorecard
    shape the web app renders)."""
    if not pairs:
        return []
    probs, labels = _split(pairs)
    out: list[dict[str, float | int]] = []
    for b in reliability_bins(probs, labels, n_bins=n_bins):
        if b.count and b.mean_confidence is not None and b.accuracy is not None:
            out.append(
                {
                    "confidence": round(b.mean_confidence, 4),
                    "accuracy": round(b.accuracy, 4),
                    "count": b.count,
                    "lo": b.lo,
                    "hi": b.hi,
                }
            )
    return out


def reliability_png(pairs: Sequence[Pair], path: Path, *, title: str) -> Path | None:
    if not pairs:
        return None
    probs, labels = _split(pairs)
    bins = reliability_bins(probs, labels, n_bins=N_BINS)
    return plot_reliability(bins, path, title=title, ece_value=_ece(probs, labels))
