"""Cost and latency (DESIGN §5.6, §7): $/doc from ``usage`` x the price table (as recorded on
every call and in the ``extractions`` table), escalation rates, p50/p95 latency."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

import numpy as np

__all__ = [
    "TIERS",
    "cost_from_extractions",
    "escalation_rates",
    "percentile",
    "usd_per_doc",
]

TIERS = ("haiku", "sonnet", "opus")


def usd_per_doc(costs: Sequence[Decimal | str]) -> Decimal | None:
    if not costs:
        return None
    total = sum((Decimal(str(c)) for c in costs), Decimal(0))
    return total / len(costs)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (numpy default); ``None`` for an empty sample."""
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def cost_from_extractions(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum ``extractions`` rows (``tier``, ``cost_usd``, ``latency_ms`` and token columns) per
    tier. Returns ``{"by_tier": {tier: {...}}, "total_usd": Decimal, "calls": n}``."""
    by: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    calls = 0
    for r in rows:
        t = str(r["tier"])
        d = by.setdefault(
            t,
            {
                "calls": 0,
                "usd": Decimal(0),
                "latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
            },
        )
        usd = Decimal(str(r.get("cost_usd") or 0))
        d["calls"] += 1
        d["usd"] += usd
        d["latency_ms"] += int(r.get("latency_ms") or 0)
        for k in ("input_tokens", "output_tokens", "cache_read_tokens"):
            d[k] += int(r.get(k) or 0)
        total += usd
        calls += 1
    return {"by_tier": by, "total_usd": total, "calls": calls}


def escalation_rates(tiers_per_doc: Sequence[Sequence[str]]) -> dict[str, float | None]:
    """Share of extracted documents that reached Sonnet and Opus (replaces the §7.3 30%/5%
    assumptions with measured values)."""
    docs = [set(t) for t in tiers_per_doc if t]
    if not docs:
        return {"sonnet": None, "opus": None}
    return {
        "sonnet": sum("sonnet" in d for d in docs) / len(docs),
        "opus": sum("opus" in d for d in docs) / len(docs),
    }
