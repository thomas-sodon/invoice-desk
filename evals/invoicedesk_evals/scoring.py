"""Turn one :class:`DocRun` plus its gold label into a :class:`DocScore`, and aggregate scores
into the scorecard metrics (one flat ``{metric: value}`` dict, so bootstrap resampling can
re-run exactly the same function)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from invoicedesk.config.models import ModelRole, settings_for
from invoicedesk.contracts.extraction import HEADER_FIELDS, REQUIRED_FIELDS, InvoiceExtraction
from invoicedesk.masking import mask_text
from invoicedesk.status import DocStatus
from invoicedesk_data.gold import GoldRecord

from invoicedesk_evals.metrics import calibration, cost
from invoicedesk_evals.metrics.boxes import score_boxes
from invoicedesk_evals.metrics.fields import Counts, score_header_fields
from invoicedesk_evals.metrics.line_items import Line, align_lines
from invoicedesk_evals.metrics.stp import (
    adversarial_miss,
    bank_change_hold_recall,
    doc_exact_match,
    false_stp_rate,
    injection_fpr,
    injection_recall,
    is_false_stp,
    rule_counts,
    scored_rules,
    stp_rate,
)
from invoicedesk_evals.model import DocRun, DocScore, EvalExample, FieldScore

__all__ = ["HEADLINE_METRICS", "aggregate", "details", "score_run"]

_READY = DocStatus.READY_TO_PAY.value
_FLAGGED = DocStatus.FLAGGED.value


def _lines(ex: InvoiceExtraction) -> list[Line]:
    return [Line(li.description.value, li.amount.value_cents) for li in ex.line_items]


def _stored(name: str, v: str | int | None) -> str | int | None:
    if isinstance(v, str):
        # Remit-to text carries account numbers: never store or print them unmasked.
        return mask_text(v) if name == "remit_to_bank_details" else v
    return v


def score_run(
    run: DocRun,
    gold: GoldRecord,
    example: EvalExample,
    *,
    required_fields: Sequence[str] = REQUIRED_FIELDS,
) -> DocScore:
    exp = gold.expected
    expected_status = DocStatus(exp.status).value
    illegible = list(gold.truth.illegible_fields)
    gold_ex = gold.invoice_extraction()
    pred_ex = (
        InvoiceExtraction.model_validate(run.extraction) if run.extraction is not None else None
    )
    processed = run.error is None
    # A document the run should have read but did not (harness error, refusal, missing
    # extract step) is scored as *unread*: every gold field counts as a miss. Dropping it
    # would let a regression that makes hard documents crash *raise* field F1. Documents the
    # run quarantined, or that the label expects quarantined, are never read and not scored.
    extracted = not run.quarantined and (pred_ex is not None or not exp.quarantined)

    fields: dict[str, FieldScore] = {}
    required_correct: bool | None = None
    lines = Counts()
    n_gold_lines = len(gold_ex.line_items)
    n_pred_lines = 0
    calib: list[tuple[float, bool]] = []
    if extracted:
        outcomes = score_header_fields(
            gold_ex,
            pred_ex,
            gold_vendor_id=gold.truth.vendor_id,
            pred_vendor_id=run.vendor_id,
            vendor_resolved=run.vendor_resolved,
            exclude=illegible,
        )
        for name, o in outcomes.items():
            fields[name] = FieldScore(
                tp=o.counts.tp,
                fp=o.counts.fp,
                fn=o.counts.fn,
                correct=o.correct,
                gold=_stored(name, o.gold),
                pred=_stored(name, o.pred),
            )
            if name != "doc_type" and o.pred is not None and o.correct is not None:
                p = run.calibrated.get(name)
                if p is not None:
                    calib.append((float(p), bool(o.correct)))
        required_correct = all(
            outcomes[n].correct in (True, None) for n in required_fields if n in outcomes
        )
        pred_lines = _lines(pred_ex) if pred_ex is not None else []
        align = align_lines(_lines(gold_ex), pred_lines)
        lines = align.counts
        n_pred_lines = len(pred_lines)

    failed = dict(run.failed_rules)
    injection_positive = bool(exp.quarantined or "R10" in exp.rules)
    injection_detected = bool(run.quarantined or "R10" in failed)
    bank_positive = "R6" in exp.rules
    bank_held = run.status == _FLAGGED and ("R6" in failed or run.quarantined)
    is_case = example.is_adversarial_case
    boxes = score_boxes(
        {k: v.model_dump(mode="json") for k, v in gold.boxes.items()},
        run.boxes,
        medium=gold.medium,
        box_eval=gold.box_eval and extracted and pred_ex is not None and run.has_boxes,
        skip=illegible,
    )
    return DocScore(
        example_id=example.example_id,
        doc_id=example.doc_id,
        set=example.set,
        split=example.split,
        medium=example.medium,
        category=example.category,
        config=run.config,
        repeat=run.repeat,
        status=run.status,
        expected_status=expected_status,
        auto_posted=run.auto_posted,
        clean_truth=expected_status == _READY,
        quarantined=run.quarantined,
        expected_quarantined=bool(exp.quarantined),
        extracted=extracted,
        error=run.error,
        fields=fields,
        required_correct=required_correct,
        lines_tp=lines.tp,
        lines_fp=lines.fp,
        lines_fn=lines.fn,
        lines_gold=n_gold_lines if extracted else 0,
        lines_pred=n_pred_lines,
        rules_expected=dict(exp.rules),
        rules_failed=failed,
        rules_scored=scored_rules(quarantined=run.quarantined, processed=processed),
        injection_positive=injection_positive,
        injection_detected=injection_detected,
        bank_change_positive=bank_positive,
        bank_change_held=bank_held,
        adversarial_case=is_case,
        adversarial_miss=adversarial_miss(
            is_case=is_case,
            status=run.status,
            expected_status=expected_status,
            expected_rules=exp.rules,
            failed_rules=failed,
            quarantined=run.quarantined,
        ),
        false_stp=is_false_stp(
            auto_posted=run.auto_posted,
            required_correct=required_correct,
            expected_status=expected_status,
        ),
        calib=calib,
        box_hits=boxes.hits,
        box_total=boxes.total,
        cost_usd=run.cost_usd,
        cost_by_tier=_cost_by_tier(run),
        latency_ms=run.latency_ms,
        tiers=list(run.tiers),
        illegible=illegible,
    )


def _cost_by_tier(run: DocRun) -> dict[str, str]:
    by: dict[str, Decimal] = {}
    for c in run.calls:
        try:
            tier = settings_for(ModelRole(c.role)).tier
        except (ValueError, KeyError):
            tier = "other"
        by[tier] = by.get(tier, Decimal(0)) + Decimal(c.cost_usd)
    return {t: str(v) for t, v in sorted(by.items())}


_TIER_ORDER = {"haiku": 0, "sonnet": 1, "opus": 2}


# ----------------------------------------------------------------------------- aggregation
#: Metrics shown in the scorecard, report and gate (all bootstrapped).
HEADLINE_METRICS: tuple[str, ...] = (
    "field_f1",
    "field_precision",
    "field_recall",
    "line_item_f1",
    "doc_exact_match",
    "stp",
    "false_stp",
    "injection_recall",
    "injection_fpr",
    "bank_change_hold_recall",
    "duplicate_recall",
    "duplicate_precision",
    "adversarial_misses",
    "ece",
    "box_hit_rate",
    "usd_per_doc",
    "p50_s",
    "p95_s",
)


def aggregate(scores: Sequence[DocScore]) -> dict[str, float | None]:
    """Point values of every headline metric over ``scores`` (all repeats pooled)."""
    extracted = [s for s in scores if s.extracted]
    fc = Counts()
    for s in extracted:
        for f in s.fields.values():
            fc = fc + Counts(f.tp, f.fp, f.fn)
    lc = Counts(
        sum(s.lines_tp for s in extracted),
        sum(s.lines_fp for s in extracted),
        sum(s.lines_fn for s in extracted),
    )
    rules = rule_counts(scores)
    pairs = [p for s in scores for p in s.calib]
    box_total = sum(s.box_total for s in scores)
    lat = [s.latency_ms / 1000.0 for s in scores if s.error is None]
    usd = cost.usd_per_doc([s.cost_usd for s in scores])
    cases = [s for s in scores if s.adversarial_case]
    return {
        "field_f1": fc.f1,
        "field_precision": fc.precision,
        "field_recall": fc.recall,
        "line_item_f1": lc.f1,
        "doc_exact_match": doc_exact_match(scores),
        "stp": stp_rate(scores),
        "false_stp": false_stp_rate(scores),
        "injection_recall": injection_recall(scores),
        "injection_fpr": injection_fpr(scores),
        "bank_change_hold_recall": bank_change_hold_recall(scores),
        "duplicate_recall": rules["R4"].recall,
        "duplicate_precision": rules["R4"].precision,
        "adversarial_misses": _case_misses(cases) if cases else None,
        "ece": calibration.ece(pairs),
        "box_hit_rate": (sum(s.box_hits for s in scores) / box_total) if box_total else None,
        "usd_per_doc": float(usd) if usd is not None else None,
        "p50_s": cost.percentile(lat, 50),
        "p95_s": cost.percentile(lat, 95),
    }


def _case_misses(cases: Sequence[DocScore]) -> float:
    """Adversarial cases missed in at least one repeat (a case is missed or it is not).

    A bootstrap resample can draw the same case several times (each draw brings all of its
    repeats), so each missed case counts once per draw. Counting distinct ids instead would
    bias every resample low, and the CI would not even contain the point value.
    """
    by: dict[str, list[DocScore]] = {}
    for s in cases:
        by.setdefault(s.example_id, []).append(s)
    n = 0
    for items in by.values():
        if any(s.adversarial_miss for s in items):
            n += len(items) // max(1, len({s.repeat for s in items}))
    return float(n)


def _counts_json(c: Counts) -> dict[str, Any]:
    return {
        "tp": c.tp,
        "fp": c.fp,
        "fn": c.fn,
        "precision": c.precision,
        "recall": c.recall,
        "f1": c.f1,
    }


def details(scores: Sequence[DocScore]) -> dict[str, Any]:
    """Breakdowns that are published but not bootstrapped: per-field P/R/F1, rule P/R, cost
    by tier, escalation rates, per-medium field F1, adversarial misses, reliability bins."""
    extracted = [s for s in scores if s.extracted]
    per_field: dict[str, Counts] = {}
    for s in extracted:
        for n, f in s.fields.items():
            per_field[n] = per_field.get(n, Counts()) + Counts(f.tp, f.fp, f.fn)
    per_field = {n: per_field[n] for n in HEADER_FIELDS if n in per_field}
    by_medium: dict[str, Counts] = {}
    for s in extracted:
        c = by_medium.get(s.medium, Counts())
        for f in s.fields.values():
            c = c + Counts(f.tp, f.fp, f.fn)
        by_medium[s.medium] = c
    total_usd = sum((Decimal(s.cost_usd) for s in scores), Decimal(0))
    tier_usd: dict[str, Decimal] = {}
    for s in scores:
        for t, v in s.cost_by_tier.items():
            tier_usd[t] = tier_usd.get(t, Decimal(0)) + Decimal(v)
    n = len(scores)
    return {
        "n_docs": len({s.example_id for s in scores}),
        "n_results": len(scores),
        "n_extracted": len(extracted),
        "n_quarantined": sum(s.quarantined for s in scores),
        "n_errors": sum(s.error is not None for s in scores),
        "per_field": {n: _counts_json(c) for n, c in per_field.items()},
        "field_f1_by_medium": {m: c.f1 for m, c in sorted(by_medium.items())},
        "rules": {r: _counts_json(c) for r, c in rule_counts(scores).items()},
        "escalation": cost.escalation_rates([s.tiers for s in scores]),
        # $/doc split by model tier (sums to usd_per_doc for Claude-only configs)
        "usd_per_doc_by_tier": {
            t: str(tier_usd[t] / n) for t in sorted(tier_usd, key=lambda k: _TIER_ORDER.get(k, 9))
        }
        if n
        else {},
        "total_usd": str(total_usd),
        "status_counts": _status_counts(scores),
        "adversarial_misses": sorted(
            {s.example_id for s in scores if s.adversarial_case and s.adversarial_miss}
        ),
        "false_stp_docs": sorted({s.example_id for s in scores if s.false_stp}),
        "reliability": calibration.reliability([p for s in scores for p in s.calib]),
        "fields_considered": list(HEADER_FIELDS),
    }


def _status_counts(scores: Iterable[DocScore]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in scores:
        out[s.status] = out.get(s.status, 0) + 1
    return dict(sorted(out.items()))
