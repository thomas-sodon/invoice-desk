"""Bootstrap confidence intervals and the paired bootstrap test (DESIGN §5.6).

Model outputs are non-deterministic, so every config runs n=3 repeats. The resampling unit is
the *document* (a cluster bootstrap): a resample draws documents with replacement and keeps all
repeats of each drawn document, so the CI covers both document sampling and run-to-run noise
without treating repeats as independent documents. Seeded ``numpy`` generators make every CI
reproducible.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

import numpy as np

__all__ = [
    "DEFAULT_RESAMPLES",
    "PairedResult",
    "bootstrap_ci",
    "paired_bootstrap",
    "percentile_ci",
]

DEFAULT_RESAMPLES = 1000

T = TypeVar("T")
Metrics = dict[str, float | None]
MetricFn = Callable[[Sequence[T]], Metrics]


def _clusters[T](
    items: Sequence[T], key: Callable[[T], str]
) -> tuple[list[str], dict[str, list[T]]]:
    groups: dict[str, list[T]] = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    return sorted(groups), groups


def percentile_ci(values: Sequence[float], *, alpha: float = 0.05) -> tuple[float, float] | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    lo, hi = np.percentile(arr, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def bootstrap_ci[T](
    items: Sequence[T],
    fn: MetricFn[T],
    *,
    key: Callable[[T], str],
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, tuple[float, float] | None]:
    """Percentile CI for every metric ``fn`` returns, resampling clusters by ``key``."""
    ids, groups = _clusters(items, key)
    if not ids:
        return {}
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {}
    for _ in range(n_resamples):
        drawn = rng.integers(0, len(ids), size=len(ids))
        resample = [it for i in drawn for it in groups[ids[int(i)]]]
        for name, v in fn(resample).items():
            if v is not None:
                samples.setdefault(name, []).append(float(v))
    names = fn(list(items)).keys()
    return {n: percentile_ci(samples.get(n, []), alpha=alpha) for n in names}


@dataclass(frozen=True)
class PairedResult:
    metric: str
    a: float | None
    b: float | None
    delta: float | None  # b - a on the full paired sample
    ci: tuple[float, float] | None  # bootstrap CI of the delta
    p_value: float | None  # two-sided bootstrap p-value for delta == 0
    n_pairs: int


def paired_bootstrap[T](
    items_a: Sequence[T],
    items_b: Sequence[T],
    fn: MetricFn[T],
    metrics: Sequence[str],
    *,
    key: Callable[[T], str],
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    alpha: float = 0.05,
) -> list[PairedResult]:
    """Paired bootstrap of ``fn(b) - fn(a)`` over the documents both configs ran.

    Each resample draws the same documents for both configs, which removes between-document
    variance from the comparison. The p-value is ``2 * min(P(d <= 0), P(d >= 0))`` over the
    resampled deltas (capped at 1).
    """
    ids_a, ga = _clusters(items_a, key)
    _, gb = _clusters(items_b, key)
    ids = [i for i in ids_a if i in gb]
    pa = [it for i in ids for it in ga[i]]
    pb = [it for i in ids for it in gb[i]]
    full_a, full_b = fn(pa), fn(pb)
    rng = np.random.default_rng(seed)
    deltas: dict[str, list[float]] = {m: [] for m in metrics}
    if ids:
        for _ in range(n_resamples):
            drawn = [ids[int(i)] for i in rng.integers(0, len(ids), size=len(ids))]
            ma = fn([it for i in drawn for it in ga[i]])
            mb = fn([it for i in drawn for it in gb[i]])
            for m in metrics:
                va, vb = ma.get(m), mb.get(m)
                if va is not None and vb is not None:
                    deltas[m].append(float(vb) - float(va))
    out: list[PairedResult] = []
    for m in metrics:
        va, vb = full_a.get(m), full_b.get(m)
        d = deltas[m]
        p: float | None = None
        if d:
            arr = np.asarray(d)
            p = float(min(1.0, 2 * min(np.mean(arr <= 0), np.mean(arr >= 0))))
        out.append(
            PairedResult(
                metric=m,
                a=va,
                b=vb,
                delta=None if va is None or vb is None else float(vb) - float(va),
                ci=percentile_ci(d, alpha=alpha),
                p_value=p,
                n_pairs=len(ids),
            )
        )
    return out
