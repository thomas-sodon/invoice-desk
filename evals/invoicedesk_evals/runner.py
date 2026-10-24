"""``invoicedesk-eval``: the eval harness CLI (DESIGN §5.6, §7.3, §8 days 5/17/18).

Subcommands::

    run             run matrix config(s) over a split, n repeats, write eval_runs/eval_results,
                    dated JSON + markdown reports (bootstrap CIs, paired bootstrap D vs A),
                    root-cause labels + spot-check sheet, optional badge and Report Card
    fit-thresholds  choose tau_field / tau_hi on the DEV split only
    gate            PR regression gate against a baseline from main
    baseline        write a compact gate baseline from a report
    report-card     render the AP Automation Report Card PDF from the latest eval_runs row
    public          SROIE / CORD public-benchmark track (skipped without the download cache)
    estimate        print the cost estimate for a run without running it
    smoke-list      print the smoke-set example ids

Money safety: offline (the default, and ``--offline``) answers come from the data component's
deterministic ``OracleLLM`` and cost $0. A live run (``INVOICEDESK_MODE=live`` or ``--live``)
always prints a cost estimate first and refuses to start without ``--i-know-this-costs-money``;
the full matrix (``--config all``) additionally needs ``--full-matrix`` (about $40).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from invoicedesk.config.settings import REPO_ROOT, Settings, get_settings
from invoicedesk.config.tenant import RulesConfig, load_rules_config
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.db.connection import Database
from invoicedesk.db.migrate import apply_migrations

from invoicedesk_evals import store
from invoicedesk_evals.datasets import (
    Example,
    assert_not_test,
    filter_media,
    load_split,
    smoke_dev_ids,
)
from invoicedesk_evals.matrix import ConfigSpec, load_matrix
from invoicedesk_evals.model import DocRun, DocScore
from invoicedesk_evals.pg import PgHost, PostgresUnavailable, start_ephemeral
from invoicedesk_evals.report import (
    BENCHMARK_LABEL,
    REPORTS_DIR,
    config_report,
    paired_report,
    write_badge,
    write_report,
)
from invoicedesk_evals.scoring import score_run

__all__ = ["RunOutcome", "estimate_usd", "execute", "main"]

log = logging.getLogger("invoicedesk_evals")

RESULTS_DB = "invoicedesk_eval_results"
#: Root-cause labeling estimate: Opus, effort high, Batch API (half price) per failure.
ROOT_CAUSE_USD_PER_FAILURE = Decimal("0.015")
ROOT_CAUSE_FAILURE_SHARE = Decimal("0.30")
FULL_MATRIX = ("A", "B", "C", "D", "D'", "E", "F", "G", "N")


# ----------------------------------------------------------------------------- helpers
def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def git_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - developer tool on PATH
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GITHUB_SHA", "unknown")


def parse_configs(text: str) -> list[ConfigSpec]:
    m = load_matrix()
    names = list(m.configs) if text.strip().lower() == "all" else text.split(",")
    return [m.get(n) for n in names if n.strip()]


def estimate_usd(
    specs: Sequence[ConfigSpec],
    examples: Sequence[Example],
    *,
    repeats: int,
    root_cause_live: bool = False,
) -> tuple[Decimal, list[str]]:
    total = Decimal(0)
    lines: list[str] = []
    for s in specs:
        n = len(filter_media(examples, s.media))
        usd = s.est_usd_per_doc * n * repeats
        if root_cause_live and s.pipeline == "invoicedesk":
            usd += ROOT_CAUSE_USD_PER_FAILURE * ROOT_CAUSE_FAILURE_SHARE * n * repeats
        total += usd
        lines.append(
            f"  {s.name:3s} {n:4d} docs x {repeats} x ${s.est_usd_per_doc}/doc = ${usd:.2f}"
        )
    return total, lines


def _is_full_matrix(specs: Sequence[ConfigSpec]) -> bool:
    return set(FULL_MATRIX) - {"G"} <= {s.name for s in specs}


@dataclass
class RunOutcome:
    report: dict[str, Any]
    json_path: Path | None
    md_path: Path | None
    runs: dict[str, list[DocRun]] = field(default_factory=dict)
    scores: dict[str, list[DocScore]] = field(default_factory=dict)
    examples: list[Example] = field(default_factory=list)


class MoneyGuardError(RuntimeError):
    pass


def check_money_guard(
    *,
    live: bool,
    specs: Sequence[ConfigSpec],
    examples: Sequence[Example],
    repeats: int,
    i_know: bool,
    full_matrix: bool,
    root_cause_live: bool,
    out: Any = None,
) -> Decimal:
    out = out or sys.stderr
    usd, lines = estimate_usd(specs, examples, repeats=repeats, root_cause_live=root_cause_live)
    if not live:
        print(
            f"offline run (OracleLLM): estimated cost $0 (a live run would be ~${usd:.2f})",
            file=out,
        )
        return Decimal(0)
    print("LIVE eval run: cost estimate (DESIGN §7.3 per-doc budgets)", file=out)
    for ln in lines:
        print(ln, file=out)
    print(f"  total ~${usd:.2f}", file=out)
    if not i_know:
        raise MoneyGuardError(
            "refusing a live run without --i-know-this-costs-money (estimate above)"
        )
    if _is_full_matrix(specs) and not full_matrix:
        raise MoneyGuardError(
            "the full matrix is ~$40: re-run with --full-matrix after confirming with the owner"
        )
    return usd


def _results_db(host: PgHost, dsn: str | None) -> str:
    if dsn:
        apply_migrations(dsn)
        return dsn
    try:
        rdsn = host.create_db(RESULTS_DB)
    except Exception:  # already exists on a reused server
        rdsn = host.dsn_for(RESULTS_DB)
    apply_migrations(rdsn)
    return rdsn


def _llm(live: bool, settings: Settings) -> LLMClient:
    if live:
        from invoicedesk import wiring

        if settings.mode != "live":
            settings = settings.model_copy(update={"mode": "live"})
        return wiring.llm_client(settings)
    from invoicedesk_data.oracle import OracleLLM

    return OracleLLM.from_gold(("synthetic-core", "adversarial"))


def _progress(spec: ConfigSpec, repeat: int, total: int) -> Any:
    state = {"n": 0, "t0": time.monotonic()}

    def done(run: DocRun) -> None:
        state["n"] += 1
        if state["n"] % 10 == 0 or state["n"] == total:
            el = time.monotonic() - state["t0"]
            print(
                f"  [{spec.name} r{repeat}] {state['n']}/{total} docs ({el:.0f}s)",
                file=sys.stderr,
                flush=True,
            )

    return done


# ----------------------------------------------------------------------------- execute
def execute(  # noqa: PLR0915
    *,
    configs: Sequence[ConfigSpec],
    split: str,
    repeats: int,
    live: bool,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    dsn: str | None = None,
    results_dsn: str | None = None,
    host: PgHost | None = None,
    workers: int = 1,
    seed: int = 0,
    n_resamples: int = 1000,
    thresholds: dict[str, Any] | None = None,
    out_dir: Path | None = REPORTS_DIR,
    limit: int | None = None,
    root_cause: bool = True,
    root_cause_llm: LLMClient | None = None,
    badge: bool = False,
    report_card: Path | None = None,
    stem: str | None = None,
    today: dt.date | None = None,
    examples: Sequence[Example] | None = None,
    preprocessor: Any = None,
) -> RunOutcome:
    """Run configs over a split and write everything. Callers handle the money guard."""
    from invoicedesk_data.universe import AS_OF

    from invoicedesk_evals.harness import Harness
    from invoicedesk_evals.root_cause import breakdown, label_failures, write_spot_check_sheet

    s = settings or get_settings()
    exs = list(examples) if examples is not None else load_split(split)
    if limit:
        exs = exs[:limit]
    rules_cfg: RulesConfig = load_rules_config(s.tenant, s.tenants_dir)
    th_info: dict[str, Any] | None = None
    if thresholds is not None:
        from invoicedesk_evals.thresholds import apply_thresholds

        rules_cfg = apply_thresholds(rules_cfg, thresholds)
        th_info = {
            "tau_field": thresholds["tau_field"],
            "tau_hi": thresholds["tau_hi"],
            "source": "dev-fitted thresholds file",
        }
    else:
        th_info = {
            "tau_field": rules_cfg.confidence.tau_field,
            "tau_hi": rules_cfg.confidence.tau_hi,
            "source": f"tenants/{s.tenant}/rules.yaml",
        }
    mode = "live" if live else "offline"
    client = llm or _llm(live, s)
    own_host = host is None
    if host is None:
        host = PgHost.from_dsn(dsn) if dsn else start_ephemeral()
    started = dt.datetime.now(dt.UTC)
    sha = git_sha()
    runs: dict[str, list[DocRun]] = {}
    scores: dict[str, list[DocScore]] = {}
    skipped: dict[str, str] = {}
    pre = preprocessor
    shared_storage: Any = None
    try:
        rdsn = _results_db(host, results_dsn or dsn)
        for spec in configs:
            ex_c = filter_media(exs, spec.media)
            reason = _skip_reason(spec, live)
            if reason is not None:
                skipped[spec.name] = reason
                print(f"config {spec.name}: skipped ({reason})", file=sys.stderr)
                continue
            print(
                f"config {spec.name}: {spec.description} - {len(ex_c)} docs x {repeats}",
                file=sys.stderr,
            )
            h = Harness(
                host,
                spec,
                client,
                rules_config=rules_cfg,
                today=today or AS_OF,
                simulated_latency=not live,
                workers=workers,
                preprocessor=pre,
                storage=shared_storage,
            )
            pre = h.preprocessor  # reuse the preprocessing cache across configs
            shared_storage = h.storage  # (the cache copies renders into this storage)
            if spec.extractor == "azure_di":
                from invoicedesk_evals.azure_invoice import AzureInvoiceExtractor

                h.extractor = AzureInvoiceExtractor(h.storage)
            with h:
                cfg_runs: list[DocRun] = []
                for r in range(repeats):
                    cfg_runs += h.run(ex_c, repeat=r, on_done=_progress(spec, r, len(ex_c)))
            by_id = {e.id: e for e in ex_c}
            runs[spec.name] = cfg_runs
            scores[spec.name] = [
                score_run(
                    run,
                    by_id[run.example_id].gold,
                    by_id[run.example_id].example,
                    required_fields=rules_cfg.confidence.required_fields,
                )
                for run in cfg_runs
            ]
        # root causes
        rc: dict[str, dict[str, int]] = {}
        rc_labels: dict[str, Any] = {}
        if root_cause:
            for name, sc in scores.items():
                labels = label_failures(sc, llm=root_cause_llm)
                rc_labels[name] = labels
                rc[name] = breakdown(labels)
                for i, s_ in enumerate(sc):
                    lbl = labels.get((s_.example_id, s_.repeat))
                    if lbl is not None:
                        sc[i] = s_.model_copy(update={"root_cause": lbl[0].label})
        # reports
        cfg_reports: list[dict[str, Any]] = []
        for spec in configs:
            if spec.name in skipped:
                cfg_reports.append(
                    config_report(spec, [], n_resamples=0, seed=seed, skipped=skipped[spec.name])
                )
                continue
            rep = config_report(spec, scores[spec.name], n_resamples=n_resamples, seed=seed)
            if spec.name in rc:
                rep["root_causes"] = rc[spec.name]
            cfg_reports.append(rep)
        paired = []
        for a, b in (("A", "D"), ("N", "D")):
            if a in scores and b in scores:
                paired.append(
                    paired_report(
                        scores[a],
                        scores[b],
                        base_name=a,
                        other_name=b,
                        n_resamples=n_resamples,
                        seed=seed,
                    )
                )
        finished = dt.datetime.now(dt.UTC)
        primary = "D" if "D" in scores else (next(iter(scores), None))
        report: dict[str, Any] = {
            "schema": "invoicedesk.eval-report/v1",
            "label": BENCHMARK_LABEL,
            "mode": mode,
            "oracle": not live,
            "split": split,
            "git_sha": sha,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "n_repeats": repeats,
            "n_resamples": n_resamples,
            "seed": seed,
            "thresholds": th_info,
            "primary_config": primary,
            "runs": cfg_reports,
            "paired": paired,
            "eval_runs": [],
        }
        stem = stem or _stem(split, configs, mode, started)
        json_path = md_path = None
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            for spec in configs:
                sc = scores.get(spec.name)
                if not sc:
                    continue
                from invoicedesk_evals.metrics.calibration import reliability_png

                reliability_png(
                    [p for d in sc for p in d.calib],
                    out_dir / f"{stem}-{_safe(spec.name)}-reliability.png",
                    title=f"Config {spec.name} reliability ({mode}, {BENCHMARK_LABEL})",
                )
                if spec.name in rc_labels:
                    write_spot_check_sheet(
                        out_dir / f"{stem}-{_safe(spec.name)}-root-cause-spot-check.csv",
                        sc,
                        rc_labels[spec.name],
                        seed=seed,
                    )
        # eval_runs rows (primary config last, so "latest" is the production config)
        db = Database(rdsn, min_size=1, max_size=2)
        try:
            order = [c for c in scores if c != primary] + ([primary] if primary else [])
            matrix_summary = [
                {
                    "config": r["config"],
                    "description": r["description"],
                    "metrics": r.get("metrics", {}),
                }
                for r in cfg_reports
                if not r.get("skipped")
            ]
            n_cases = len({e.id for e in exs if e.example.is_adversarial_case})
            for name in order:
                rep = next(r for r in cfg_reports if r["config"] == name)
                with db.tx() as conn:
                    ids = store.ensure_examples(
                        conn, filter_media(exs, load_matrix().get(name).media)
                    )
                    run_id = store.start_run(
                        conn,
                        git_sha=sha,
                        config=name,
                        split=split,
                        mode=mode,
                        n_repeats=repeats,
                    )
                    store.add_results(conn, run_id, ids, scores[name])
                    totals = {
                        "label": BENCHMARK_LABEL,
                        "split": split,
                        "mode": mode,
                        "oracle": not live,
                        "n_repeats": repeats,
                        "metrics": rep["metrics"],
                        "details": rep["details"],
                        "reliability": rep["details"]["reliability"],
                        "root_causes": rep.get("root_causes", {}),
                        "matrix": matrix_summary,
                        "adversarial_cases": n_cases,
                        "thresholds": th_info,
                        "paired": [p for p in paired if name in (p["a"], p["b"])],
                    }
                    rel = None
                    if out_dir is not None:
                        rel = _rel(out_dir / f"{stem}.json")
                    store.finish_run(conn, run_id, totals, report_path=rel)
                    row = conn.execute(
                        "select id, git_sha, config, split, mode, n_repeats, started_at,"
                        " finished_at, totals_json from eval_runs where id = %s",
                        (run_id,),
                    ).fetchone()
                    assert row is not None
                    snap = {
                        k: (v.isoformat() if isinstance(v, dt.datetime) else v)
                        for k, v in dict(row).items()
                    }
                    snap["id"] = str(snap["id"])
                    report["eval_runs"].append(snap)
                    rep["run_id"] = str(run_id)
        finally:
            db.close()
        if out_dir is not None:
            json_path, md_path = write_report(report, out_dir, stem)
            if badge and primary is not None:
                prim = next(r for r in cfg_reports if r["config"] == primary)
                write_badge(prim, mode=mode, path=out_dir / "badge.json")
        if report_card is not None and report["eval_runs"]:
            build_card_from_row(report["eval_runs"][-1], report_card, s)
        return RunOutcome(report, json_path, md_path, runs, scores, exs)
    finally:
        if own_host:
            host.stop()


def _skip_reason(spec: ConfigSpec, live: bool) -> str | None:
    if spec.extractor == "azure_di":
        if not live:
            return "Azure DI arm runs live only (no offline replay of a commercial API)"
        missing = spec.missing_env()
        if missing:
            return "missing " + ", ".join(missing)
    return None


def _safe(name: str) -> str:
    return name.replace("'", "prime")


def _stem(split: str, configs: Sequence[ConfigSpec], mode: str, when: dt.datetime) -> str:
    names = "-".join(_safe(c.name) for c in configs)
    return f"{when.date().isoformat()}-{split.replace('+', '_')}-{names}-{mode}"


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def build_card_from_row(row: dict[str, Any], out: Path, settings: Settings | None = None) -> Path:
    from invoicedesk_evals.report_card import card_context, render_html, render_pdf

    s = settings or get_settings()
    cfg = load_rules_config(s.tenant, s.tenants_dir)
    ctx = card_context(
        row,
        minutes_per_invoice=cfg.hours_saved_per_invoice_min,
        manual_cost_cents=cfg.manual_cost_per_invoice_cents,
    )
    html = render_html(ctx)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".html").write_text(html, encoding="utf-8")
    return render_pdf(html, out)


# ----------------------------------------------------------------------------- CLI
def _add_run_args(p: argparse.ArgumentParser, *, default_split: str) -> None:
    p.add_argument("--config", default="D", help="config(s): D | D,N | all (matrix.yaml)")
    p.add_argument(
        "--split",
        default=default_split,
        help="dev | test | adversarial | smoke | "
        "all | adversarial-cases, combinable with + (e.g. dev+adversarial)",
    )
    p.add_argument("--repeats", type=int, default=None, help="default: matrix default (3)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use the gold-backed OracleLLM ($0)")
    mode.add_argument("--live", action="store_true", help="call Claude (costs money)")
    p.add_argument("--i-know-this-costs-money", dest="i_know", action="store_true")
    p.add_argument("--full-matrix", action="store_true", help="confirm the ~$40 full matrix")
    p.add_argument(
        "--dsn",
        default=None,
        help="existing Postgres server/db (needs CREATEDB); default: a throwaway local cluster",
    )
    p.add_argument("--results-dsn", default=None, help="where eval_runs/eval_results go")
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="documents in parallel (default 1: OCR already uses every core, and parallel "
        "documents inflate the measured latency without adding throughput)",
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--resamples", type=int, default=1000)
    p.add_argument("--limit", type=int, default=None, help="first N examples only (debugging)")
    p.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    p.add_argument("--stem", default=None, help="report file stem (default: dated)")
    p.add_argument("--calibrator", type=Path, default=None, help="fitted calibrator artifact")


def _live(args: argparse.Namespace, s: Settings) -> bool:
    if args.offline:
        return False
    return bool(args.live or s.mode == "live")


def _cmd_run(args: argparse.Namespace) -> int:
    s = get_settings()
    specs = parse_configs(args.config)
    repeats = args.repeats or load_matrix().default_repeats
    live = _live(args, s)
    exs = load_split(args.split)
    if args.limit:
        exs = exs[: args.limit]
    thresholds = None
    if args.thresholds:
        from invoicedesk_evals.thresholds import load_thresholds

        thresholds = load_thresholds(args.thresholds)
    if args.calibrator:
        os.environ["INVOICEDESK_CALIBRATOR"] = str(args.calibrator)
    root_cause_live = live and not args.root_cause_offline
    check_money_guard(
        live=live,
        specs=specs,
        examples=exs,
        repeats=repeats,
        i_know=args.i_know,
        full_matrix=args.full_matrix,
        root_cause_live=root_cause_live,
    )
    rc_llm = _llm(True, s) if root_cause_live else None
    try:
        out = execute(
            configs=specs,
            split=args.split,
            repeats=repeats,
            live=live,
            settings=s,
            dsn=args.dsn,
            results_dsn=args.results_dsn,
            workers=args.workers,
            seed=args.seed,
            n_resamples=args.resamples,
            thresholds=thresholds,
            out_dir=args.out_dir,
            examples=exs,
            root_cause=not args.no_root_cause,
            root_cause_llm=rc_llm,
            badge=args.badge,
            report_card=args.report_card,
            stem=args.stem,
        )
    except PostgresUnavailable as exc:
        print(f"error: {exc} (pass --dsn to use an existing server)", file=sys.stderr)
        return 2
    if args.langfuse:
        from invoicedesk_evals.langfuse_push import langfuse_configured, push_results

        if langfuse_configured(s):
            for row in out.report["eval_runs"]:
                res = push_results(
                    s,
                    run_id=row["id"],
                    config=row["config"],
                    split=args.split,
                    scores=out.scores[row["config"]],
                )
                print(f"langfuse: {row['config']} {res}", file=sys.stderr)
        else:
            print("langfuse: not configured; skipped", file=sys.stderr)
    print(f"report: {out.json_path}\n        {out.md_path}")
    for r in out.report["runs"]:
        if r.get("skipped"):
            print(f"  {r['config']}: skipped ({r['skipped']})")
            continue
        m = r["metrics"]
        print(
            f"  {r['config']}: field F1 {_v(m, 'field_f1')}  STP {_v(m, 'stp')}  "
            f"false-STP {_v(m, 'false_stp')}  adversarial misses {_v(m, 'adversarial_misses')}"
            f"  $/doc {_v(m, 'usd_per_doc')}  p95 {_v(m, 'p95_s')}s"
        )
    if args.report_card:
        print(f"report card: {args.report_card}")
    return 0


def _v(m: dict[str, Any], k: str) -> str:
    v = (m.get(k) or {}).get("value")
    return "n/a" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))


def _cmd_fit_thresholds(args: argparse.Namespace) -> int:
    assert_not_test(args.split, "fit thresholds")  # before anything runs
    s = get_settings()
    specs = parse_configs(args.config)
    if len(specs) != 1:
        print("fit-thresholds takes exactly one config", file=sys.stderr)
        return 2
    live = _live(args, s)
    repeats = args.repeats or 1
    exs = load_split(args.split)
    check_money_guard(
        live=live,
        specs=specs,
        examples=exs,
        repeats=repeats,
        i_know=args.i_know,
        full_matrix=args.full_matrix,
        root_cause_live=False,
    )
    out = execute(
        configs=specs,
        split=args.split,
        repeats=repeats,
        live=live,
        settings=s,
        dsn=args.dsn,
        results_dsn=args.results_dsn,
        workers=args.workers,
        seed=args.seed,
        n_resamples=args.resamples,
        out_dir=args.out_dir,
        examples=exs,
        root_cause=False,
        stem=args.stem,
    )
    from invoicedesk_evals.thresholds import fit_thresholds

    name = specs[0].name
    cfg = load_rules_config(s.tenant, s.tenants_dir)
    choice = fit_thresholds(
        out.runs[name],
        out.scores[name],
        split=args.split,
        required=cfg.confidence.required_fields,
        max_false_stp=args.max_false_stp,
    )
    choice |= {"config": name, "mode": "live" if live else "offline", "git_sha": git_sha()}
    dest = args.out or (args.out_dir / f"thresholds-{args.split}-{_today().isoformat()}.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(choice, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(choice, indent=1))
    print(f"wrote {dest}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from invoicedesk_evals.gate import evaluate_gate, load_metrics, write_step_summary

    current = load_metrics(args.current, args.config)
    if args.baseline is None or not Path(args.baseline).exists():
        if not args.baseline_missing_ok:
            print(f"baseline {args.baseline} not found", file=sys.stderr)
            return 2
        print(
            "warning: no baseline from main; relative checks compare against the current run",
            file=sys.stderr,
        )
        baseline = current
    else:
        baseline = load_metrics(args.baseline, args.config)
    res = evaluate_gate(current, baseline, config=args.config)
    text = res.markdown()
    print(text)
    write_step_summary(text)
    return 0 if res.passed else 1


def _cmd_baseline(args: argparse.Namespace) -> int:
    from invoicedesk_evals.gate import compact_baseline

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    out = compact_baseline(report, args.config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def _latest_report(d: Path) -> Path | None:
    cands = sorted(
        (
            p
            for p in d.glob("*.json")
            if p.name not in ("badge.json",)
            and not p.name.startswith(("thresholds-", "public-", "baseline"))
        ),
        key=lambda p: p.stat().st_mtime,
    )
    return cands[-1] if cands else None


def _cmd_report_card(args: argparse.Namespace) -> int:
    from invoicedesk_evals.report_card import row_from_report

    row: dict[str, Any] | None = None
    if args.dsn:
        db = Database(args.dsn, min_size=1, max_size=1)
        try:
            with db.tx() as conn:
                row = store.latest_run(conn, config=args.config, mode=args.mode)
        finally:
            db.close()
        if row is None:
            print("no finished eval_runs row found", file=sys.stderr)
            return 2
    else:
        path = args.report or _latest_report(REPORTS_DIR)
        if path is None:
            print("no report JSON found; run `invoicedesk-eval run` first", file=sys.stderr)
            return 2
        row = row_from_report(json.loads(Path(path).read_text(encoding="utf-8")), args.config)
    out = build_card_from_row(row, args.out)
    print(f"wrote {out}")
    return 0


def _cmd_public(args: argparse.Namespace) -> int:
    from invoicedesk_evals.public_track import run_public_source

    s = get_settings()
    live = _live(args, s)
    spec = parse_configs(args.config)[0]
    sources = ["sroie", "cord"] if args.source == "all" else [args.source]
    if not live:
        print(
            "public track: offline mode has no answers for SROIE/CORD receipts (the oracle "
            "only knows the synthetic gold); skipped. Use --live.",
            file=sys.stderr,
        )
        return 0
    from invoicedesk_data.public.load import load_public

    n = sum(len(load_public(src)[: args.limit or None]) for src in sources)
    usd = spec.est_usd_per_doc * n
    print(
        f"LIVE public track: {n} receipts x ${spec.est_usd_per_doc} = ~${usd:.2f}", file=sys.stderr
    )
    if n and not args.i_know:
        print("refusing without --i-know-this-costs-money", file=sys.stderr)
        return 2
    client = _llm(True, s)
    results = [
        run_public_source(src, spec, client, limit=args.limit, n_resamples=args.resamples)
        for src in sources
    ]
    out = {
        "label": "public benchmark (SROIE / CORD; images not redistributed)",
        "git_sha": git_sha(),
        "date": _today().isoformat(),
        "results": results,
    }
    dest = args.out_dir / f"public-{_today().isoformat()}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
    for r in results:
        if r.get("skipped"):
            print(f"  {r['source']}: skipped ({r['skipped']})")
        else:
            print(f"  {r['source']}: field F1 {r['field_f1']['value']} over {r['n_docs']} docs")
    print(f"wrote {dest}")
    return 0


def _cmd_estimate(args: argparse.Namespace) -> int:
    specs = parse_configs(args.config)
    repeats = args.repeats or load_matrix().default_repeats
    exs = load_split(args.split)
    usd, lines = estimate_usd(specs, exs, repeats=repeats, root_cause_live=True)
    for ln in lines:
        print(ln)
    print(f"  total ~${usd:.2f} (live; offline runs cost $0)")
    return 0


def _cmd_smoke_list(_: argparse.Namespace) -> int:
    from invoicedesk_data.gold import load_gold

    dev = load_gold("synthetic-core", split="dev")
    for i in smoke_dev_ids(dev):
        print(f"synthetic-core/{i}")
    for r in load_gold("adversarial"):
        if r.category not in (None, "prior"):
            print(f"adversarial/{r.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    ap = argparse.ArgumentParser(
        prog="invoicedesk-eval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run config(s) over a split")
    _add_run_args(run, default_split=load_matrix().default_split)
    run.add_argument("--thresholds", type=Path, default=None, help="dev-fitted thresholds JSON")
    run.add_argument("--no-root-cause", action="store_true")
    run.add_argument(
        "--root-cause-offline",
        action="store_true",
        help="heuristic root-cause labels even on a live run",
    )
    run.add_argument("--badge", action="store_true", help="write evals/reports/badge.json")
    run.add_argument(
        "--report-card",
        type=Path,
        nargs="?",
        const=REPO_ROOT / "docs" / "report-card" / "AP-Automation-Report-Card.pdf",
        default=None,
    )
    run.add_argument("--langfuse", action="store_true", help="push results to Langfuse")
    run.set_defaults(func=_cmd_run)

    fit = sub.add_parser("fit-thresholds", help="choose tau_field/tau_hi on DEV only")
    _add_run_args(fit, default_split="dev")
    fit.add_argument("--max-false-stp", type=float, default=0.01)
    fit.add_argument("--out", type=Path, default=None)
    fit.set_defaults(func=_cmd_fit_thresholds)

    gate = sub.add_parser("gate", help="PR regression gate")
    gate.add_argument("--current", type=Path, required=True)
    gate.add_argument("--baseline", type=Path, default=None)
    gate.add_argument("--baseline-missing-ok", action="store_true")
    gate.add_argument("--config", default="D")
    gate.set_defaults(func=_cmd_gate)

    base = sub.add_parser("baseline", help="write a compact gate baseline from a report")
    base.add_argument("--report", type=Path, required=True)
    base.add_argument("--config", default="D")
    base.add_argument("--out", type=Path, required=True)
    base.set_defaults(func=_cmd_baseline)

    card = sub.add_parser("report-card", help="render the AP Automation Report Card PDF")
    card.add_argument("--dsn", default=None, help="read the latest eval_runs row from here")
    card.add_argument("--report", type=Path, default=None, help="or from this report JSON")
    card.add_argument("--config", default=None)
    card.add_argument("--mode", choices=["live", "offline"], default=None)
    card.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "report-card" / "AP-Automation-Report-Card.pdf",
    )
    card.set_defaults(func=_cmd_report_card)

    pub = sub.add_parser("public", help="SROIE / CORD public-benchmark track")
    pub.add_argument("--source", choices=["sroie", "cord", "all"], default="all")
    pub.add_argument("--config", default="D")
    pub.add_argument("--limit", type=int, default=None)
    pub.add_argument("--resamples", type=int, default=1000)
    pub.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    m = pub.add_mutually_exclusive_group()
    m.add_argument("--offline", action="store_true")
    m.add_argument("--live", action="store_true")
    pub.add_argument("--i-know-this-costs-money", dest="i_know", action="store_true")
    pub.set_defaults(func=_cmd_public)

    est = sub.add_parser("estimate", help="print a live-run cost estimate")
    est.add_argument("--config", default="D")
    est.add_argument("--split", default="test")
    est.add_argument("--repeats", type=int, default=None)
    est.set_defaults(func=_cmd_estimate)

    sm = sub.add_parser("smoke-list", help="print the smoke-set example ids")
    sm.set_defaults(func=_cmd_smoke_list)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("INVOICEDESK_EVAL_LOG", "WARNING"))
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except MoneyGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except PermissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
