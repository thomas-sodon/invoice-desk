"""Owner dashboard (M12, US-6), comparison mode (M13, US-10), scorecard and report card
(M17, M21, US-11). Every metric here is labelled "benchmark on synthetic data" (CLAUDE.md,
Scope discipline): the data is the fictional Maple Row universe."""

from __future__ import annotations

import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from invoicedesk.pipeline.events import IN_FLIGHT

from invoicedesk_api import schemas as sc
from invoicedesk_api.access import batch_row, is_public_batch
from invoicedesk_api.auth import OptionalStaffDep, RuntimeDep, StaffDep
from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime
from invoicedesk_api.views import iso, rules_config, usd

router = APIRouter()

LABEL = "benchmark on synthetic data"
#: Label for a scorecard built from an offline (gold-backed oracle) run: it measures the
#: harness and the deterministic rules, never a model.
OFFLINE_LABEL = "benchmark on synthetic data (offline harness run, not a live model measurement)"
_HELD = ("NeedsReview", "Flagged", "Rejected", "Quarantined")


# ---------------------------------------------------------------------------- dashboard
def dashboard_numbers(rt: Runtime, *, days: int) -> sc.Dashboard:
    cfg = rules_config(rt)
    since = rt.clock() - dt.timedelta(days=days)
    with rt.db.tx() as conn:
        docs = conn.execute(
            "select d.id, d.status::text as status, d.total_cost_usd,"
            " exists (select 1 from audit_log a where a.document_id = d.id"
            "   and a.action = 'status.changed'"
            "   and a.after_json->>'status' in ('NeedsReview', 'Flagged')) as touched,"
            " (select f.value from fields f where f.document_id = d.id and f.final"
            "   and f.name = 'total') as total"
            " from documents d where d.tenant_id = %s and d.session_id is null"
            " and d.created_at >= %s and not (d.status::text = any(%s))",
            (rt.tenant, since, sorted(IN_FLIGHT)),
        ).fetchall()
        ids = [str(d["id"]) for d in docs]
        rules: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in ids}
        for r in conn.execute(
            "select r.document_id, r.rule_id, r.passed, r.details_json from rule_results r"
            " where r.document_id = any(%s::uuid[]) and r.evaluation = (select max(r2.evaluation)"
            " from rule_results r2 where r2.document_id = r.document_id)",
            (ids,),
        ).fetchall():
            rules[str(r["document_id"])][r["rule_id"]] = dict(r)
        quarantined = {
            str(r["document_id"])
            for r in conn.execute(
                "select distinct document_id from audit_log where document_id = any(%s::uuid[])"
                " and action = 'status.changed' and after_json->>'status' = 'Quarantined'",
                (ids,),
            ).fetchall()
        }
    invoices = len(docs)
    hands_free = sum(
        1 for d in docs if d["status"] in ("ReadyToPay", "Posted") and not d["touched"]
    )
    dup = fraud = over = 0
    for d in docs:
        if d["status"] not in _HELD:
            continue  # released or paid: nothing was caught
        did = str(d["id"])
        failed = {k: v for k, v in rules[did].items() if not v["passed"]}
        total = int(d["total"]) if d["total"] and str(d["total"]).lstrip("-").isdigit() else 0
        if "R4" in failed:
            dup += total
        elif (
            "R6" in failed
            or "R10" in failed
            or ("R1" in failed and _lookalike(failed["R1"]))
            or did in quarantined
        ):
            fraud += total
        if "R3" in failed:
            variance = (failed["R3"]["details_json"] or {}).get("variance_cents")
            if isinstance(variance, int) and variance > 0:
                over += variance
    cost = sum((Decimal(d["total_cost_usd"]) for d in docs), Decimal(0))
    per = (cost / invoices).quantize(Decimal("0.0001"), ROUND_HALF_UP) if invoices else Decimal(0)
    minutes = cfg.hours_saved_per_invoice_min
    return sc.Dashboard(
        invoices=invoices,
        hands_free_pct=round(100.0 * hands_free / invoices, 1) if invoices else 0.0,
        hours_saved=round(hands_free * minutes / 60.0, 1),
        dollars_caught_cents=sc.DollarsCaught(
            duplicates=dup, fraud=fraud, overcharges=over, total=dup + fraud + over
        ),
        cost_per_invoice_usd=usd(per),
        manual_benchmark_per_invoice_cents=cfg.manual_cost_per_invoice_cents,
        assumptions={"minutes_per_invoice": minutes},
        label=LABEL,
    )


def _lookalike(row: dict[str, Any]) -> bool:
    return (row["details_json"] or {}).get("badge") == "lookalike"


@router.get("/dashboard", response_model=sc.Dashboard)
def dashboard(
    *,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.Dashboard:
    return dashboard_numbers(rt, days=days)


# ---------------------------------------------------------------------------- comparison
def _naive_missing(rt: Runtime, batch_id: str) -> bool:
    with rt.db.tx() as conn:
        row = conn.execute(
            "select count(*) filter (where c.id is null) as missing,"
            " count(*) filter (where d.status::text = any(%s)) as busy"
            " from documents d left join comparison_results c on c.document_id = d.id"
            " and c.pipeline = 'naive' where d.batch_id = %s and d.session_id is null",
            (sorted(IN_FLIGHT), batch_id),
        ).fetchone()
    return row is not None and int(row["missing"]) > 0 and int(row["busy"]) == 0


@router.get("/batches/{batch_id}/comparison", response_model=sc.Comparison)
async def comparison(
    *,
    batch_id: str,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> sc.Comparison:
    """Runs the naive config-N baseline (dry run, once per batch, after the batch settled) and
    grades both pipelines on the same documents."""

    def _work() -> dict[str, Any]:
        from invoicedesk.pipeline.naive import run_naive

        with rt.db.tx() as conn:
            batch = batch_row(conn, rt, batch_id)
        if staff is None and not is_public_batch(batch):
            raise ApiError(401, "unauthenticated", "Sign in to see this batch.")
        bid = str(batch["id"])
        if _naive_missing(rt, bid):
            kind = rt.pipeline_kind_for_batch(bid)
            if staff is None and kind != "default":
                # Anonymous callers never cause paid LLM calls (US-8 is always the recorded
                # run): the naive baseline of a live demo batch runs on the replay oracle.
                kind = "demo_replay"
            svc = rt.services(kind)
            run_naive(rt.db, bid, svc.deps.llm, storage=rt.storage)
        truth = demo_truth(rt, bid) if is_public_batch(batch) else None
        return rt.services().comparison(bid, truth=truth)

    out = await run_in_threadpool(_work)
    return sc.Comparison.model_validate(out)


def svc_compare(rt: Runtime, batch_id: str) -> dict[str, Any]:
    return rt.services().comparison(batch_id)


#: Planted-defect rules in ``data/demo/expected.json`` -> comparison money-risk labels.
_RULE_LABELS = {"R4": "duplicate", "R6": "bank_change", "R10": "injection"}


def _demo_labels_by_sha() -> dict[str, set[str]]:
    from invoicedesk.config.settings import REPO_ROOT

    path = REPO_ROOT / "data" / "demo" / "expected.json"
    try:
        expected = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {
        f["sha256"]: {_RULE_LABELS[r] for r in f.get("rules", {}) if r in _RULE_LABELS}
        for f in expected.get("files", [])
    }


def demo_truth(rt: Runtime, batch_id: str) -> dict[str, list[str]] | None:
    """Gold money-risk labels for a demo batch, keyed by document id (matched on the file's
    SHA-256), so comparison mode is graded against the labels and not against InvoiceDesk's
    own findings. ``None`` (fall back to evidence) when any document is not a known demo file."""
    labels = _demo_labels_by_sha()
    with rt.db.tx() as conn:
        rows = conn.execute(
            "select id, sha256 from documents where batch_id = %s and session_id is null",
            (batch_id,),
        ).fetchall()
    truth: dict[str, list[str]] = {}
    for r in rows:
        found = labels.get(r["sha256"])
        if found is None:
            return None
        truth[str(r["id"])] = sorted(found)
    return truth


# ---------------------------------------------------------------------------- scorecard
def _metric(v: Any) -> Any:
    if isinstance(v, dict) and "value" in v:
        return v
    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
        return {"value": v, "ci": None}
    return v


@router.get("/scorecard/latest", response_model=sc.Scorecard)
def scorecard(rt: RuntimeDep) -> sc.Scorecard:
    """The scorecard row (public: the portfolio scorecard page): the latest finished live run
    of config D when one exists, else the latest live run, else the latest offline run, which
    is labelled as an offline harness run so oracle numbers are never shown as a benchmark."""
    with rt.db.tx() as conn:
        row = conn.execute(
            "select id, git_sha, config, mode, finished_at, totals_json from eval_runs"
            " where finished_at is not null order by (mode = 'live') desc, (config = 'D') desc,"
            " finished_at desc, started_at desc limit 1"
        ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No eval run has finished yet.")
    totals = dict(row["totals_json"] or {})
    raw = totals.get("metrics")
    metrics = (
        dict(raw)
        if isinstance(raw, dict)
        else {k: v for k, v in totals.items() if k not in ("reliability", "label")}
    )
    reliability = totals.get("reliability")
    return sc.Scorecard(
        run_id=str(row["id"]),
        git_sha=row["git_sha"],
        config=row["config"],
        finished_at=iso(row["finished_at"]) or "",
        metrics={k: _metric(v) for k, v in metrics.items()},
        label=LABEL if row["mode"] == "live" else OFFLINE_LABEL,
        reliability=reliability if isinstance(reliability, list) else None,
    )


def latest_report_card(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    pdfs = [p for p in directory.rglob("*.pdf") if "report" in p.name.lower() and p.is_file()]
    return max(pdfs, key=lambda p: (p.stat().st_mtime, p.name)) if pdfs else None


@router.get("/report-card.pdf")
def report_card(rt: RuntimeDep) -> FileResponse:
    path = latest_report_card(Path(rt.api.report_card_dir))
    if path is None:
        raise ApiError(404, "not_found", "No report card has been generated yet.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="InvoiceDesk-AP-Automation-Report-Card.pdf",
    )
