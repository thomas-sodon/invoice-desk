"""Run reports: dated JSON + markdown in ``evals/reports/``, the reliability PNG and the
shields.io ``badge.json`` (DESIGN §5.6, M17, M19).

Every number carries the label "benchmark on synthetic data". Offline runs (the gold-backed
oracle stands in for Claude) are marked as such everywhere: they test the harness, the pipeline
wiring and the rules, not model accuracy, cost or latency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from invoicedesk_evals.matrix import ConfigSpec
from invoicedesk_evals.model import DocScore
from invoicedesk_evals.scoring import HEADLINE_METRICS, aggregate, details
from invoicedesk_evals.stats import PairedResult, bootstrap_ci, paired_bootstrap

__all__ = [
    "BENCHMARK_LABEL",
    "OFFLINE_NOTE",
    "PAIRED_METRICS",
    "REPORTS_DIR",
    "TARGETS",
    "config_report",
    "meets_target",
    "paired_report",
    "render_markdown",
    "write_badge",
    "write_report",
]

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
BENCHMARK_LABEL = "benchmark on synthetic data"
OFFLINE_NOTE = (
    "Offline run: model answers come from the deterministic gold-backed OracleLLM (with "
    "synthetic per-tier noise), so these numbers exercise the harness, pipeline wiring, "
    "screening and rules. They are not measurements of Claude's accuracy, cost or latency."
)

#: DESIGN §5.6 targets: (comparison, threshold). "ge" = at least, "le" = at most.
TARGETS: dict[str, tuple[str, float]] = {
    "field_f1": ("ge", 0.96),
    "line_item_f1": ("ge", 0.90),
    "doc_exact_match": ("ge", 0.85),
    "stp": ("ge", 0.70),
    "false_stp": ("le", 0.01),
    "duplicate_recall": ("ge", 1.0),
    "duplicate_precision": ("ge", 0.95),
    "injection_recall": ("ge", 1.0),
    "injection_fpr": ("le", 0.03),
    "bank_change_hold_recall": ("ge", 1.0),
    "adversarial_misses": ("le", 0.0),
    "ece": ("le", 0.05),
    "box_hit_rate": ("ge", 0.90),
    "usd_per_doc": ("le", 0.015),
    "p50_s": ("le", 4.0),
    "p95_s": ("le", 12.0),
}

PAIRED_METRICS = ("field_f1", "stp", "false_stp", "usd_per_doc", "p95_s")

_LABELS = {
    "field_f1": "Header-field micro F1",
    "field_precision": "Header-field precision",
    "field_recall": "Header-field recall",
    "line_item_f1": "Line-item F1",
    "doc_exact_match": "Document exact match",
    "stp": "STP rate",
    "false_stp": "False-STP rate",
    "injection_recall": "Injection recall",
    "injection_fpr": "Injection FPR (clean docs)",
    "bank_change_hold_recall": "Bank-change hold recall",
    "duplicate_recall": "Duplicate (R4) recall",
    "duplicate_precision": "Duplicate (R4) precision",
    "adversarial_misses": "Adversarial misses (30 cases)",
    "ece": "ECE (10 bins)",
    "box_hit_rate": "Box hit rate (IoU >= 0.5)",
    "usd_per_doc": "$ per document",
    "p50_s": "Latency p50 (s)",
    "p95_s": "Latency p95 (s)",
}


def meets_target(metric: str, value: float | None) -> bool | None:
    t = TARGETS.get(metric)
    if t is None or value is None:
        return None
    op, thr = t
    return value >= thr - 1e-12 if op == "ge" else value <= thr + 1e-12


def _key(s: DocScore) -> str:
    return s.example_id


def config_report(
    spec: ConfigSpec,
    scores: Sequence[DocScore],
    *,
    n_resamples: int,
    seed: int,
    skipped: str | None = None,
) -> dict[str, Any]:
    """Metrics with 95% cluster-bootstrap CIs plus breakdowns for one config."""
    if skipped is not None or not scores:
        return {
            "config": spec.name,
            "description": spec.description,
            "skipped": skipped or "no results",
            "metrics": {},
        }
    point = aggregate(scores)
    cis = bootstrap_ci(scores, aggregate, key=_key, n_resamples=n_resamples, seed=seed)
    metrics: dict[str, Any] = {}
    for m in HEADLINE_METRICS:
        v = point.get(m)
        ci = cis.get(m)
        metrics[m] = {
            "value": v,
            "ci": list(ci) if ci is not None else None,
            "target": _target_text(m),
            "meets_target": meets_target(m, v),
        }
    return {
        "config": spec.name,
        "description": spec.description,
        "metrics": metrics,
        "details": details(scores),
        "results": [_result_row(s) for s in scores],
    }


def _result_row(s: DocScore) -> dict[str, Any]:
    """Compact per-(example, repeat) outcome; field values are not included (eval_results
    holds the full, masked scores)."""
    return {
        "example_id": s.example_id,
        "repeat": s.repeat,
        "medium": s.medium,
        "status": s.status,
        "expected_status": s.expected_status,
        "rules_failed": s.rules_failed,
        "rules_expected": s.rules_expected,
        "wrong_fields": sorted(n for n, f in s.fields.items() if f.correct is False),
        "false_stp": s.false_stp,
        "adversarial_miss": s.adversarial_miss,
        "tiers": s.tiers,
        "cost_usd": s.cost_usd,
        "latency_ms": s.latency_ms,
        "root_cause": s.root_cause,
        "error": s.error,
    }


def _target_text(m: str) -> str | None:
    t = TARGETS.get(m)
    if t is None:
        return None
    return f"{'>=' if t[0] == 'ge' else '<='} {t[1]:g}"


def paired_report(
    base: Sequence[DocScore],
    other: Sequence[DocScore],
    *,
    base_name: str,
    other_name: str,
    n_resamples: int,
    seed: int,
) -> dict[str, Any]:
    res: list[PairedResult] = paired_bootstrap(
        base,
        other,
        aggregate,
        PAIRED_METRICS,
        key=_key,
        n_resamples=n_resamples,
        seed=seed,
    )
    return {
        "a": base_name,
        "b": other_name,
        "n_pairs": res[0].n_pairs if res else 0,
        "metrics": {
            r.metric: {
                "a": r.a,
                "b": r.b,
                "delta": r.delta,
                "ci": list(r.ci) if r.ci is not None else None,
                "p_value": r.p_value,
            }
            for r in res
        },
    }


# ----------------------------------------------------------------------------- writing
def _fmt(metric: str, v: Any) -> str:
    if v is None:
        return "n/a"
    if metric == "usd_per_doc":
        return f"${float(v):.4f}"
    if metric in ("p50_s", "p95_s"):
        return f"{float(v):.2f}"
    if metric == "adversarial_misses":
        return f"{int(v)}"
    return f"{float(v):.3f}"


def _fmt_ci(metric: str, ci: Sequence[float] | None) -> str:
    if not ci:
        return ""
    return f"[{_fmt(metric, ci[0])}, {_fmt(metric, ci[1])}]"


def render_markdown(report: Mapping[str, Any]) -> str:  # noqa: PLR0915
    lines: list[str] = []
    lines.append(f"# InvoiceDesk eval: {report['split']} ({report['started_at'][:10]})")
    lines.append("")
    lines.append(
        f"*{BENCHMARK_LABEL}* · mode **{report['mode']}** · git `{report['git_sha']}` · "
        f"n={report['n_repeats']} repeats · {report['n_resamples']} bootstrap resamples "
        f"(seed {report['seed']})"
    )
    lines.append("")
    if report["mode"] == "offline":
        lines.append(f"> **{OFFLINE_NOTE}**")
        lines.append("")
    if report.get("thresholds"):
        th = report["thresholds"]
        lines.append(
            f"Thresholds: tau_field={th.get('tau_field')}, tau_hi={th.get('tau_hi')} "
            f"(source: {th.get('source')})."
        )
        lines.append("")
    runs: list[Mapping[str, Any]] = report["runs"]
    lines.append("## Summary by config")
    lines.append("")
    cols = ["field_f1", "stp", "false_stp", "adversarial_misses", "usd_per_doc", "p95_s"]
    lines.append("| Config | " + " | ".join(_LABELS[c] for c in cols) + " |")
    lines.append("|---|" + "---|" * len(cols))
    for r in runs:
        if r.get("skipped"):
            lines.append(f"| {r['config']} | skipped: {r['skipped']} |" + " |" * (len(cols) - 1))
            continue
        cells = []
        for c in cols:
            m = r["metrics"].get(c, {})
            ci = _fmt_ci(c, m.get("ci"))
            cells.append(f"{_fmt(c, m.get('value'))} {ci}".strip())
        lines.append(f"| {r['config']} | " + " | ".join(cells) + " |")
    lines.append("")
    for p in report.get("paired", []):
        lines.append(f"## Paired bootstrap: {p['b']} vs {p['a']} ({p['n_pairs']} paired documents)")
        lines.append("")
        lines.append("| Metric | " + p["a"] + " | " + p["b"] + " | delta (b - a) | 95% CI | p |")
        lines.append("|---|---|---|---|---|---|")
        for m, v in p["metrics"].items():
            pv = "n/a" if v["p_value"] is None else f"{v['p_value']:.3f}"
            lines.append(
                f"| {_LABELS.get(m, m)} | {_fmt(m, v['a'])} | {_fmt(m, v['b'])} | "
                f"{_fmt(m, v['delta']) if v['delta'] is not None else 'n/a'} | "
                f"{_fmt_ci(m, v['ci'])} | {pv} |"
            )
        lines.append("")
    for r in runs:
        if r.get("skipped"):
            continue
        d = r["details"]
        lines.append(f"## Config {r['config']}: {r['description']}")
        lines.append("")
        lines.append(
            f"{d['n_docs']} documents, {d['n_results']} results, {d['n_extracted']} extracted, "
            f"{d['n_quarantined']} quarantined, {d['n_errors']} errors. Total cost "
            f"${float(d['total_usd']):.4f}. Escalation: Sonnet "
            f"{_pct(d['escalation'].get('sonnet'))}, Opus {_pct(d['escalation'].get('opus'))}."
        )
        lines.append("")
        by_tier = d.get("usd_per_doc_by_tier") or {}
        if by_tier:
            lines.append(
                "$/doc by tier: "
                + ", ".join(f"{t} ${float(v):.4f}" for t, v in by_tier.items())
                + "."
            )
            lines.append("")
        lines.append("| Metric | Value | 95% CI | Target | Meets |")
        lines.append("|---|---|---|---|---|")
        for m, v in r["metrics"].items():
            meets = {True: "yes", False: "**no**", None: ""}[v.get("meets_target")]
            lines.append(
                f"| {_LABELS.get(m, m)} | {_fmt(m, v['value'])} | {_fmt_ci(m, v['ci'])} | "
                f"{v.get('target') or ''} | {meets} |"
            )
        lines.append("")
        lines.append("Per-field P/R/F1:")
        lines.append("")
        lines.append("| Field | P | R | F1 | TP | FP | FN |")
        lines.append("|---|---|---|---|---|---|---|")
        for f, c in d["per_field"].items():
            lines.append(
                f"| {f} | {_f3(c['precision'])} | {_f3(c['recall'])} | {_f3(c['f1'])} | "
                f"{c['tp']} | {c['fp']} | {c['fn']} |"
            )
        lines.append("")
        lines.append("Rule P/R vs planted labels:")
        lines.append("")
        lines.append("| Rule | P | R | TP | FP | FN |")
        lines.append("|---|---|---|---|---|---|")
        for rule, c in d["rules"].items():
            lines.append(
                f"| {rule} | {_f3(c['precision'])} | {_f3(c['recall'])} | {c['tp']} | "
                f"{c['fp']} | {c['fn']} |"
            )
        lines.append("")
        if d["adversarial_misses"]:
            lines.append("Adversarial misses: " + ", ".join(d["adversarial_misses"]))
            lines.append("")
        if d["false_stp_docs"]:
            lines.append("False-STP documents: " + ", ".join(d["false_stp_docs"]))
            lines.append("")
        if r.get("root_causes"):
            lines.append("Root-cause breakdown of failures:")
            lines.append("")
            lines.append("| Root cause | Count |")
            lines.append("|---|---|")
            for k, n in r["root_causes"].items():
                lines.append(f"| {k} | {n} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _f3(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def write_report(report: Mapping[str, Any], out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / f"{stem}.json"
    mp = out_dir / f"{stem}.md"
    jp.write_text(json.dumps(report, indent=1, sort_keys=False, default=str) + "\n", "utf-8")
    mp.write_text(render_markdown(report), "utf-8")
    return jp, mp


def write_badge(run: Mapping[str, Any], *, mode: str, path: Path) -> Path:
    """shields.io endpoint JSON (``https://img.shields.io/endpoint?url=...``)."""
    m = run.get("metrics", {})
    f1 = (m.get("field_f1") or {}).get("value")
    fstp = (m.get("false_stp") or {}).get("value")
    if mode != "live":
        badge = {
            "schemaVersion": 1,
            "label": "eval (synthetic)",
            "message": "offline harness run - no live numbers yet",
            "color": "lightgrey",
        }
    else:
        ok = meets_target("field_f1", f1) and meets_target("false_stp", fstp)
        msg = (
            f"field F1 {f1:.3f} | false-STP {100 * fstp:.1f}%"
            if f1 is not None and (fstp is not None)
            else "n/a"
        )
        badge = {
            "schemaVersion": 1,
            "label": "eval (synthetic data)",
            "message": msg,
            "color": "brightgreen" if ok else "orange",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(badge, indent=1) + "\n", "utf-8")
    return path
