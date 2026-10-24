"""Fit ``tau_field`` / ``tau_hi`` on the dev split only (CLAUDE.md, Evals).

This is the *only* code path in the evals component that tunes anything. It refuses any split
spec that contains ``test`` before a single document runs, and it delegates the choice to
:func:`invoicedesk.calibrate.choose_thresholds` (which refuses non-dev splits itself).

The resulting JSON is applied to later runs with ``invoicedesk-eval run --thresholds FILE``
(it overrides ``confidence.tau_field``/``tau_hi`` from ``tenants/<t>/rules.yaml`` in memory);
promoting it into ``rules.yaml`` is a reviewed change by the rules owner.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from invoicedesk.calibrate import DocScores, choose_thresholds
from invoicedesk.config.tenant import RulesConfig

from invoicedesk_evals.datasets import assert_not_test
from invoicedesk_evals.model import DocRun, DocScore

__all__ = ["apply_thresholds", "doc_scores_for_thresholds", "fit_thresholds", "load_thresholds"]


def doc_scores_for_thresholds(
    runs: Sequence[DocRun], scores: Sequence[DocScore], *, required: Sequence[str]
) -> list[DocScores]:
    by_key = {(s.example_id, s.repeat): s for s in scores}
    out: list[DocScores] = []
    for r in runs:
        s = by_key[(r.example_id, r.repeat)]
        if not s.extracted:
            continue
        probs = {n: float(r.calibrated.get(n, 0.0)) for n in required}
        correct = {n: bool(s.fields[n].correct in (True, None)) for n in required if n in s.fields}
        # Held by any rule other than R9 (the confidence gate itself), or a held gold label:
        # those documents are never auto-posted whatever the threshold.
        other = any(rule != "R9" for rule in r.failed_rules) or s.expected_status != "ReadyToPay"
        out.append(
            DocScores(
                doc_id=f"{r.example_id}#{r.repeat}",
                required_probs=probs,
                required_correct={n: correct.get(n, False) for n in probs},
                held_by_other_rules=other,
            )
        )
    return out


def fit_thresholds(
    runs: Sequence[DocRun],
    scores: Sequence[DocScore],
    *,
    split: str,
    required: Sequence[str],
    max_false_stp: float = 0.01,
) -> dict[str, Any]:
    assert_not_test(split, "fit thresholds")
    docs = doc_scores_for_thresholds(runs, scores, required=required)
    choice = choose_thresholds(docs, split=split, max_false_stp=max_false_stp)
    return {**choice.model_dump(mode="json"), "split": split, "required_fields": list(required)}


def load_thresholds(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("split") != "dev":
        raise PermissionError(
            f"{path}: thresholds must be fitted on the dev split (got {data.get('split')!r})"
        )
    return data


def apply_thresholds(cfg: RulesConfig, thresholds: dict[str, Any]) -> RulesConfig:
    conf = cfg.confidence.model_copy(
        update={"tau_field": float(thresholds["tau_field"]), "tau_hi": float(thresholds["tau_hi"])}
    )
    return cfg.model_copy(update={"confidence": conf})
