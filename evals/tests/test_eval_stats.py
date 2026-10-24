from __future__ import annotations

from collections.abc import Sequence

import pytest
from invoicedesk_evals.stats import bootstrap_ci, paired_bootstrap, percentile_ci


def _mean(xs: Sequence[tuple[str, float]]) -> dict[str, float | None]:
    return {"mean": sum(v for _, v in xs) / len(xs) if xs else None}


ITEMS = [(f"d{i}", float(i % 7)) for i in range(40)]


def test_bootstrap_is_deterministic_with_a_seed() -> None:
    a = bootstrap_ci(ITEMS, _mean, key=lambda x: x[0], n_resamples=300, seed=7)
    b = bootstrap_ci(ITEMS, _mean, key=lambda x: x[0], n_resamples=300, seed=7)
    c = bootstrap_ci(ITEMS, _mean, key=lambda x: x[0], n_resamples=300, seed=8)
    assert a == b
    assert a != c
    lo, hi = a["mean"]  # type: ignore[misc]
    point = _mean(ITEMS)["mean"]
    assert point is not None and lo <= point <= hi
    assert 0 < hi - lo < 3


def test_cluster_bootstrap_keeps_repeats_together() -> None:
    # 3 repeats of each doc with identical values: the CI equals the doc-level CI
    reps = [(k, v) for k, v in ITEMS for _ in range(3)]
    a = bootstrap_ci(ITEMS, _mean, key=lambda x: x[0], n_resamples=200, seed=1)
    b = bootstrap_ci(reps, _mean, key=lambda x: x[0], n_resamples=200, seed=1)
    assert a["mean"] == pytest.approx(b["mean"])


def test_constant_metric_has_degenerate_ci() -> None:
    items = [(f"d{i}", 1.0) for i in range(10)]
    assert bootstrap_ci(items, _mean, key=lambda x: x[0], n_resamples=50) == {"mean": (1.0, 1.0)}
    assert bootstrap_ci([], _mean, key=lambda x: x[0]) == {}
    assert percentile_ci([]) is None


def test_paired_bootstrap() -> None:
    a = [(f"d{i}", 0.5) for i in range(30)]
    same = paired_bootstrap(a, a, _mean, ["mean"], key=lambda x: x[0], n_resamples=200)
    assert same[0].delta == 0.0 and same[0].p_value == 1.0
    better = [(k, v + 0.2) for k, v in a]
    res = paired_bootstrap(a, better, _mean, ["mean"], key=lambda x: x[0], n_resamples=200)[0]
    assert res.delta == pytest.approx(0.2)
    assert res.p_value == 0.0
    assert res.ci == pytest.approx((0.2, 0.2))
    assert res.n_pairs == 30
    # unpaired documents are ignored
    partial = paired_bootstrap(a, better[:10], _mean, ["mean"], key=lambda x: x[0])
    assert partial[0].n_pairs == 10
