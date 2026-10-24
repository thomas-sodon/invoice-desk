"""AP Automation Report Card PDF (M21, US-11).

Rendered from the latest ``eval_runs`` row (``--dsn``) or from a run report JSON (which embeds
the rows it wrote) with ``docs/report-card/template.html`` (Jinja) and Playwright Chromium
(``playwright==1.63.0``, browsers in ``PLAYWRIGHT_BROWSERS_PATH``, default ``/opt/pw-browsers``).
Every number comes from the run; the card is labelled "benchmark on synthetic data" and makes
no client claims. Offline (oracle) runs carry a banner saying they are not model measurements.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from invoicedesk_evals.report import BENCHMARK_LABEL, OFFLINE_NOTE, meets_target

__all__ = [
    "CARD_PATH",
    "TEMPLATE_DIR",
    "card_context",
    "render_html",
    "render_pdf",
    "row_from_report",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "docs" / "report-card"
CARD_PATH = TEMPLATE_DIR / "AP-Automation-Report-Card.pdf"


def _metric(metrics: Mapping[str, Any], name: str) -> tuple[float | None, list[float] | None]:
    m = metrics.get(name) or {}
    v = m.get("value")
    return (None if v is None else float(v)), m.get("ci")


def _pct(v: float | None, digits: int = 0) -> str:
    return "n/a" if v is None else f"{100 * v:.{digits}f}%"


def _tone(name: str, v: float | None) -> str:
    ok = meets_target(name, v)
    return "" if ok is None else ("good" if ok else "bad")


def _ci_pct(ci: Sequence[float] | None, digits: int = 0) -> str:
    if not ci:
        return ""
    lo, hi = f"{100 * ci[0]:.{digits}f}", f"{100 * ci[1]:.{digits}f}"
    return f"{lo}%" if lo == hi else f"{lo}–{hi}%"


def _range(ci: Sequence[float] | None, digits: int = 0) -> str:
    return f" · range {_ci_pct(ci, digits)}" if ci else ""


def _short(text: str, limit: int = 44) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" -,(")
    return cut + "…"


def _reliability_svg(bins: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    x0, y0, w, h = 35.0, 10.0, 250.0, 150.0
    out: list[dict[str, float]] = []
    for b in bins:
        lo = float(b.get("lo", max(0.0, float(b["confidence"]) - 0.05)))
        hi = float(b.get("hi", min(1.0, lo + 0.1)))
        acc = float(b["accuracy"])
        bw = (hi - lo) * w * 0.86
        bx = x0 + lo * w + (hi - lo) * w * 0.07
        bh = acc * h
        out.append(
            {
                "x": round(bx, 1),
                "y": round(y0 + h - bh, 1),
                "w": round(bw, 1),
                "h": round(bh, 1),
                "cx": round(x0 + float(b["confidence"]) * w, 1),
                "cy": round(y0 + h - float(b["confidence"]) * h, 1),
            }
        )
    return out


def card_context(
    row: Mapping[str, Any],
    *,
    minutes_per_invoice: int = 8,
    manual_cost_cents: int = 990,
) -> dict[str, Any]:
    """Template context from an ``eval_runs``-shaped row (``totals_json`` holds metrics)."""
    totals = row.get("totals_json") or {}
    if isinstance(totals, str):
        totals = json.loads(totals)
    metrics: Mapping[str, Any] = totals.get("metrics") or {}
    details: Mapping[str, Any] = totals.get("details") or {}
    f1, f1_ci = _metric(metrics, "field_f1")
    stp, stp_ci = _metric(metrics, "stp")
    fstp, fstp_ci = _metric(metrics, "false_stp")
    misses, _ = _metric(metrics, "adversarial_misses")
    usd, _ = _metric(metrics, "usd_per_doc")
    p50, _ = _metric(metrics, "p50_s")
    p95, _ = _metric(metrics, "p95_s")
    ece, _ = _metric(metrics, "ece")
    n_cases = int(totals.get("adversarial_cases") or 0)
    caught = None if misses is None else n_cases - int(misses)
    tiles = [
        {
            "title": "Handled hands-free",
            "value": _pct(stp),
            "tone": _tone("stp", stp),
            "note": "of clean invoices · goal 70%+" + _range(stp_ci),
        },
        {
            "title": "Wrong auto-posts",
            "value": _pct(fstp, 1),
            "tone": _tone("false_stp", fstp),
            "note": "goal at most 1%" + _range(fstp_ci, 1),
        },
        {
            "title": "Attacks stopped",
            "value": "n/a" if caught is None or not n_cases else f"{caught} / {n_cases}",
            "tone": _tone("adversarial_misses", misses),
            "note": "fake bank changes, hidden text, duplicates",
        },
        {
            "title": "Fields read right",
            "value": _pct(f1, 1),
            "tone": _tone("field_f1", f1),
            "note": "accuracy (F1) · goal 96%+" + _range(f1_ci, 1),
        },
    ]
    rows = [
        ("Bank-detail change held for two people", "bank_change_hold_recall", "100%"),
        ("Duplicate invoices caught", "duplicate_recall", "100%"),
        ("Hidden instructions to the AI caught", "injection_recall", "100%"),
        ("Clean invoices wrongly flagged as attacks", "injection_fpr", "≤ 3%"),
        ("All key fields right on an invoice", "doc_exact_match", "85%+"),
        ("Line items read right", "line_item_f1", "90%+"),
        ("Highlight boxes on the right spot", "box_hit_rate", "90%+"),
    ]
    checks = []
    for title, name, goal in rows:
        v, ci = _metric(metrics, name)
        checks.append(
            {
                "name": title,
                "value": _pct(v, 1),
                "ci": _ci_pct(ci, 1),
                "goal": goal,
                "tone": _tone(name, v),
            }
        )
    matrix = []
    for m in totals.get("matrix") or []:
        mm = m.get("metrics") or {}
        matrix.append(
            {
                "config": m.get("config"),
                "description": _short(m.get("description") or ""),
                "f1": _pct(_metric(mm, "field_f1")[0], 1),
                "stp": _pct(_metric(mm, "stp")[0]),
                "false_stp": _pct(_metric(mm, "false_stp")[0], 1),
                "usd": "n/a"
                if _metric(mm, "usd_per_doc")[0] is None
                else f"${_metric(mm, 'usd_per_doc')[0]:.4f}",
            }
        )
    finished = row.get("finished_at")
    if isinstance(finished, dt.datetime):
        finished = finished.date().isoformat()
    hours = None if stp is None else stp * 100 * minutes_per_invoice / 60
    return {
        "label": BENCHMARK_LABEL,
        "offline_note": OFFLINE_NOTE,
        "run": {
            "run_id": str(row.get("id") or row.get("run_id") or ""),
            "git_sha": str(row.get("git_sha") or "")[:12],
            "config": row.get("config"),
            "split": row.get("split") or totals.get("split") or "",
            "mode": row.get("mode") or totals.get("mode") or "offline",
            "finished_at": str(finished or "")[:10],
            "n_docs": details.get("n_docs", "?"),
            "n_repeats": row.get("n_repeats") or totals.get("n_repeats") or 1,
            "n_cases": int(totals.get("adversarial_cases") or 0),
        },
        "tiles": tiles,
        "checks": checks,
        "matrix": matrix,
        "reliability": _reliability_svg(details.get("reliability") or []),
        "ece": "n/a" if ece is None else f"{ece:.3f}",
        "money": {
            "hours_per_100": "n/a" if hours is None else f"{hours:.1f} h",
            "usd_per_doc": "n/a" if usd is None else f"${usd:.4f}",
            "manual": f"${manual_cost_cents // 100}.{manual_cost_cents % 100:02d}",
            "latency": "n/a" if p50 is None or p95 is None else f"{p50:.1f} s / {p95:.1f} s",
            "minutes": minutes_per_invoice,
        },
    }


def render_html(ctx: Mapping[str, Any], *, template_dir: Path = TEMPLATE_DIR) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
        undefined=StrictUndefined,
    )
    return env.get_template("template.html").render(**ctx)


def render_pdf(html: str, out: Path) -> Path:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(path=str(out), format="Letter", print_background=True)
        finally:
            browser.close()
    return out


def row_from_report(report: Mapping[str, Any], config: str | None = None) -> dict[str, Any]:
    """The ``eval_runs`` row snapshot a report JSON embeds (primary config by default)."""
    rows: list[dict[str, Any]] = list(report.get("eval_runs") or [])
    if not rows:
        raise ValueError("report has no eval_runs rows")
    want = config or report.get("primary_config")
    for r in rows:
        if r.get("config") == want:
            return r
    return rows[-1]
