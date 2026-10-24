"""The harness drives the real pipeline (real preprocessor, screeners, tiered extractor,
calibrator, rules) with the gold-backed OracleLLM, in throwaway Postgres workspaces."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.config.settings import REPO_ROOT
from invoicedesk.config.tenant import load_rules_config
from invoicedesk.contracts.llm import FileImageBlock, LLMRequest, SystemBlock, TextBlock
from invoicedesk.db.connection import Database
from invoicedesk.fakes import FakeLLM
from invoicedesk_data.oracle import OracleLLM
from invoicedesk_data.universe import AS_OF
from invoicedesk_evals import store
from invoicedesk_evals.datasets import Example, load_split
from invoicedesk_evals.harness import CachingPreprocessor, ConfigLLM, Harness, LockedStorage
from invoicedesk_evals.matrix import load_matrix
from invoicedesk_evals.pg import PgHost
from invoicedesk_evals.report_card import row_from_report
from invoicedesk_evals.runner import execute, main
from invoicedesk_evals.scoring import aggregate, score_run
from pydantic import BaseModel


def _req(role: ModelRole = ModelRole.EXTRACT_T1) -> LLMRequest:
    return LLMRequest(
        role=role,
        system=(SystemBlock(text="frozen prefix " * 200, cache=True),),
        user=(FileImageBlock(file_id="file_1"), TextBlock(text="OCR words: ...")),
        output_schema="x",
        metadata={"document_id": "doc-1"},
    )


def test_config_llm_knobs() -> None:
    m = load_matrix()
    fake = FakeLLM(by_role={r.value: {} for r in ModelRole})
    a = ConfigLLM(fake, m.get("A"), simulated_latency=True)
    a.parse(_req(), _Empty)
    assert fake.calls[-1].role == ModelRole.ADJUDICATE_T3  # Opus-only: tier 1 remapped
    a.parse(_req(ModelRole.INJECTION_CLASSIFIER), _Empty)
    assert fake.calls[-1].role == ModelRole.INJECTION_CLASSIFIER  # classifier untouched
    calls = a.calls_for("doc-1")
    assert len(calls) == 2 and a.calls_for("doc-1") == []
    dp = ConfigLLM(fake, m.get("D'"), simulated_latency=True)
    dp.parse(_req(), _Empty)
    assert not any(b.cache for b in fake.calls[-1].system)
    d = ConfigLLM(fake, m.get("D"), simulated_latency=True)
    d.parse(_req(), _Empty)
    assert fake.calls[-1].role == ModelRole.EXTRACT_T1 and fake.calls[-1].system[0].cache
    # caching off costs more for the same request
    assert Decimal(dp.calls_for("doc-1")[0].cost_usd) > Decimal(d.calls_for("doc-1")[0].cost_usd)
    e = ConfigLLM(fake, m.get("E"), simulated_latency=True)
    e.parse(_req(), _Empty)
    assert all(isinstance(b, TextBlock) for b in fake.calls[-1].user)
    assert e.upload_file(b"png", filename="p.png", media_type="image/png").startswith("file_unused")


class _Empty(BaseModel):
    pass


# ----------------------------------------------------------------------------- Postgres
@pytest.fixture(scope="module")
def shared_pre() -> CachingPreprocessor:
    from invoicedesk.preprocess import build_preprocessor

    storage = LockedStorage()
    return CachingPreprocessor(build_preprocessor(storage, key_prefix="maple_row"), storage)


@pytest.fixture
def host(pg_server: Any) -> PgHost:
    return PgHost.from_dsn(pg_server.admin_dsn)


def _pick(ids: list[str]) -> list[Example]:
    exs = load_split("dev+adversarial")
    return [next(e for e in exs if e.gold.id == i) for i in ids]


@pytest.mark.postgres
@pytest.mark.slow
def test_harness_routes_like_the_labels(host: PgHost, shared_pre: CachingPreprocessor) -> None:
    exs = _pick(["core-003", "adv-001", "adv-011", "adv-020"])
    cfg = load_rules_config("maple_row", REPO_ROOT / "tenants")
    oracle = OracleLLM.from_gold(("synthetic-core", "adversarial"))
    results: dict[str, list[Any]] = {}
    for name in ("D", "N", "C", "A"):
        spec = load_matrix().get(name)
        with Harness(
            host,
            spec,
            oracle,
            rules_config=cfg,
            today=AS_OF,
            simulated_latency=True,
            preprocessor=shared_pre,
        ) as h:
            runs = h.run(exs, repeat=0)
        assert all(r.error is None for r in runs), [r.error for r in runs]
        results[name] = [score_run(r, e.gold, e.example) for r, e in zip(runs, exs, strict=True)]
        by = {r.example_id: r for r in runs}
        if name == "D":
            assert by["synthetic-core/core-003"].status == "ReadyToPay"
            assert by["adversarial/adv-001"].quarantined
            assert by["adversarial/adv-001"].tiers == []  # never extracted (invariant 8)
            assert by["adversarial/adv-011"].failed_rules.get("R6") == "flag"
            assert by["adversarial/adv-020"].failed_rules.get("R4") == "flag"  # prior ran first
            assert by["adversarial/adv-020"].vendor_resolved
            assert Decimal(by["synthetic-core/core-003"].cost_usd) > 0
            assert by["synthetic-core/core-003"].latency_ms > by["synthetic-core/core-003"].llm_ms
        if name == "C":
            assert all(set(r.tiers) <= {"haiku"} for r in runs)
        if name == "A":
            models = {c.model_id for r in runs for c in r.calls if c.role != "injection_classifier"}
            assert models == {"claude-opus-5-5"}
    # OCR ran once per file (4 examples + 1 prior) across four configs
    assert (shared_pre.misses, shared_pre.hits) == (5, 3 * 5 + 4 - 5)
    d = aggregate(results["D"])
    n = aggregate(results["N"])
    assert d["adversarial_misses"] == 0.0 and d["false_stp"] == 0.0
    assert n["adversarial_misses"] == 3.0  # the naive baseline posts every attack
    assert n["false_stp"] == pytest.approx(3 / 4)


@pytest.mark.postgres
@pytest.mark.slow
def test_execute_writes_reports_db_rows_and_report_card(
    host: PgHost, shared_pre: CachingPreprocessor, pg_dsn: str, tmp_path: Path
) -> None:
    exs = _pick(["core-003", "adv-001", "adv-020"])
    card = tmp_path / "card.pdf"
    out = execute(
        configs=[load_matrix().get("D"), load_matrix().get("N")],
        split="dev+adversarial",
        repeats=2,
        live=False,
        host=host,
        results_dsn=pg_dsn,
        examples=exs,
        n_resamples=100,
        out_dir=tmp_path,
        report_card=card,
        badge=True,
        preprocessor=shared_pre,
        stem="t",
    )
    rep = json.loads((tmp_path / "t.json").read_text())
    assert rep["label"] == "benchmark on synthetic data" and rep["mode"] == "offline"
    assert [r["config"] for r in rep["eval_runs"]] == ["N", "D"]  # primary written last
    d = next(r for r in rep["runs"] if r["config"] == "D")
    assert d["metrics"]["adversarial_misses"]["value"] == 0.0
    assert d["metrics"]["field_f1"]["ci"] is not None
    assert d["details"]["n_results"] == 6  # 3 docs x 2 repeats
    assert rep["paired"][0]["a"] == "N" and rep["paired"][0]["b"] == "D"
    assert (tmp_path / "t.md").read_text().startswith("# InvoiceDesk eval")
    assert (tmp_path / "t-D-reliability.png").exists()
    assert (tmp_path / "t-D-root-cause-spot-check.csv").exists()
    badge = json.loads((tmp_path / "badge.json").read_text())
    assert badge["schemaVersion"] == 1 and "offline" in badge["message"]
    assert card.read_bytes()[:5] == b"%PDF-"
    assert "benchmark on synthetic data" in card.with_suffix(".html").read_text().lower()
    assert len(out.scores["D"]) == 6
    db = Database(pg_dsn, min_size=1, max_size=1)
    try:
        with db.tx() as conn:
            latest = store.latest_run(conn)
            assert latest is not None and latest["config"] == "D" and latest["mode"] == "offline"
            assert latest["totals_json"]["metrics"]["false_stp"]["value"] == 0.0
            n = conn.execute(
                "select count(*) as n from eval_results where run_id = %s", (latest["id"],)
            ).fetchone()
            assert n is not None and n["n"] == 6
            assert store.latest_run(conn, mode="live") is None
    finally:
        db.close()
    # the gate passes against itself, and the report card CLI re-renders from the DB row
    assert (
        main(
            ["gate", "--current", str(tmp_path / "t.json"), "--baseline", str(tmp_path / "t.json")]
        )
        == 0
    )
    assert main(["report-card", "--dsn", pg_dsn, "--out", str(tmp_path / "c2.pdf")]) == 0
    assert (tmp_path / "c2.pdf").exists()
    assert row_from_report(rep)["config"] == "D"
