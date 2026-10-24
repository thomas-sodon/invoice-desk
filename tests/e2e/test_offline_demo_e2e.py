"""End-to-end: the 25-invoice demo batch and the bank-change e-mail through the whole offline
system (DESIGN §6.2 demo expectations, §9 beats).

Everything is real except what is external: the API app, the embedded job runner and the
LangGraph pipeline (real preprocessor and RapidOCR, hidden/faint-text detectors, tiered
extractor, calibrator, rules R1-R10, posting service) on a throwaway Postgres, with the
gold-backed OracleLLM in place of Claude and FakeAccounting in place of the QBO sandbox. A second
test drives one invoice through the ``invoicedesk-worker`` composition (offline wiring).

The module runs the demo once; the tests below read and then act on it, in file order.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from invoicedesk.audit.chain import verify
from invoicedesk.db.connection import Database
from invoicedesk.status import Actor, IllegalTransition
from invoicedesk.testing.postgres import TEMPLATE_DB, PgServer
from invoicedesk_api.testkit import (
    APPROVER,
    CLERK,
    INGEST_SECRET,
    OWNER,
    Env,
    build_env,
    unmasked_numbers,
)
from psycopg import sql

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((REPO / "data" / "demo" / "expected.json").read_text())
STATUS_BY_FILE: dict[str, str] = {f["filename"]: f["status"] for f in EXPECTED["files"]}
RULES_BY_FILE: dict[str, dict[str, str]] = {f["filename"]: f["rules"] for f in EXPECTED["files"]}
EMAIL_EXPECTED = json.loads((REPO / "data" / "demo" / "bank-change-email.json").read_text())
EXTRACTION_ROLES_TIERS = ("haiku", "sonnet", "opus")


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def e2e_db(pg_server: PgServer) -> Iterator[Database]:
    name = "e2e_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
        c.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(TEMPLATE_DB)
            )
        )
    db = Database(pg_server.dsn_for(name), min_size=1, max_size=6)
    try:
        yield db
    finally:
        db.close()
        with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
            c.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


def _sign(body: bytes) -> dict[str, str]:
    sig = hmac.new(INGEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-InvoiceDesk-Signature": f"sha256={sig}", "Content-Type": "application/json"}


def _email_payload() -> bytes:
    from invoicedesk_data.demo import bank_change_email

    mail = bank_change_email()
    payload = {
        "message_id": mail.message_id,
        "from": mail.from_addr,
        "subject": mail.subject,
        "body_text": mail.body_text,
        "received_at": "2026-10-20T09:15:00Z",
        "attachments": [
            {
                "filename": a.filename,
                "content_type": a.mime,
                "content_b64": base64.b64encode(a.data).decode(),
            }
            for a in mail.attachments
        ],
    }
    return json.dumps(payload).encode()


@pytest.fixture(scope="module")
def world(e2e_db: Database, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    env = build_env(e2e_db, tmp_path_factory.mktemp("e2e-storage"))
    # the owner starts the public demo run (demo documents carry the starter as uploader)
    run = env.client.post("/api/v1/demo/run", json={"mode": "replay"}, headers=env.dev(OWNER))
    assert run.status_code == 202, run.text
    batch_id = run.json()["batch_id"]
    raw = _email_payload()
    mail = env.client.post("/api/v1/ingest/email", content=raw, headers=_sign(raw))
    assert mail.status_code == 202, mail.text
    email_doc = mail.json()["documents"][0]["id"]
    jobs = env.drain()
    by_name = {
        d["filename"]: str(d["id"])
        for d in env.sql("select id, filename from documents where batch_id = %s", (batch_id,))
    }
    try:
        yield {
            "env": env,
            "batch_id": batch_id,
            "by_name": by_name,
            "email": email_doc,
            "jobs": jobs,
        }
    finally:
        env.client.close()
        env.rt.close()


def _env(w: dict[str, Any]) -> Env:
    return w["env"]


def _id(w: dict[str, Any], prefix: str) -> str:
    return next(v for k, v in w["by_name"].items() if k.startswith(prefix))


def _failed_rules(env: Env, doc_id: str) -> dict[str, dict[str, Any]]:
    rows = env.sql(
        "select rule_id, message, severity, details_json from rule_results where document_id = %s"
        " and not passed and evaluation = (select max(evaluation) from rule_results"
        " where document_id = %s)",
        (doc_id, doc_id),
    )
    return {r["rule_id"]: r for r in rows}


def _statuses(env: Env, doc_id: str) -> list[str]:
    rows = env.sql(
        "select after_json from audit_log where document_id = %s and action = 'status.changed'"
        " order by id",
        (doc_id,),
    )
    return [r["after_json"]["status"] for r in rows]


# ---------------------------------------------------------------------------- the board
def test_demo_batch_settles_to_18_4_3(world) -> None:
    env = _env(world)
    assert world["jobs"] == 26  # 25 demo files + the e-mail attachment
    view = env.client.get(f"/api/v1/batches/{world['batch_id']}").json()
    assert view["counts"] == {"ready_to_pay": 18, "needs_review": 4, "flagged": 3}
    got = {d["filename"]: d["status"] for d in view["documents"]}
    assert got == STATUS_BY_FILE
    headlines = {d["filename"]: d["headline"] for d in view["documents"]}
    assert headlines == {f["filename"]: f["headline"] or None for f in EXPECTED["files"]}


def test_clean_invoices_pass_every_rule(world) -> None:
    env = _env(world)
    for name, status in STATUS_BY_FILE.items():
        if status == "ReadyToPay":
            assert _failed_rules(env, world["by_name"][name]) == {}, name


def test_needs_review_cases_hold_for_the_right_reasons(world) -> None:
    env = _env(world)
    for name, status in STATUS_BY_FILE.items():
        if status != "NeedsReview":
            continue
        failed = _failed_rules(env, world["by_name"][name])
        assert set(RULES_BY_FILE[name]) <= set(failed), (name, sorted(failed))
        assert all(r["severity"] == "review" for r in failed.values()), name
    over = _failed_rules(env, _id(world, "10-rapid-rooter"))
    assert set(over) == {"R3"}
    assert over["R3"]["message"] == (
        "Rapid Rooter Plumbing invoice is $340.00 over the approved estimate ($3,200.00) "
        "for Unit 12B, WO-1187."
    )
    new_vendor = _failed_rules(env, _id(world, "11-marigold"))
    assert new_vendor["R1"]["message"].startswith("New vendor: 'Marigold Mobile Welding'")
    for prefix in ("03-thistledown", "08-handy-heron"):  # illegible handwritten totals
        failed = _failed_rules(env, _id(world, prefix))
        assert "total" in failed["R9"]["details_json"]["fields"], prefix


def test_duplicate_of_inv_2231_is_flagged(world) -> None:
    env = _env(world)
    doc = _id(world, "21-rapid-rooter")
    failed = _failed_rules(env, doc)
    assert set(failed) == {"R4"} and failed["R4"]["severity"] == "flag"
    assert failed["R4"]["message"] == "Duplicate of INV-2231 (same vendor, amount, week)."
    assert env.status(doc) == "Flagged"


@pytest.mark.parametrize(
    ("prefix", "detector"),
    [("18-ember", "hidden_text"), ("13-kettle-creek", "faint_text")],
)
def test_hidden_and_faint_text_are_quarantined_before_any_extraction(
    world, prefix: str, detector: str
) -> None:
    env = _env(world)
    doc = _id(world, prefix)
    assert _statuses(env, doc)[-2:] == ["Quarantined", "Flagged"]
    screens = env.sql("select detector, verdict from screen_results where document_id = %s", (doc,))
    assert any(s["detector"] == detector and s["verdict"] != "clean" for s in screens)
    # invariant 8: no extraction tier ever read the document, so nothing was escalated
    assert (
        env.sql("select count(*) as n from extractions where document_id = %s", (doc,))[0]["n"] == 0
    )
    detail = env.client.get(f"/api/v1/documents/{doc}", headers=env.dev(CLERK)).json()
    assert detail["status"] == "Flagged" and unmasked_numbers(detail) == []


def test_email_bank_change_note_triggers_r6(world) -> None:
    env = _env(world)
    doc = world["email"]
    assert env.status(doc) == EMAIL_EXPECTED["expected"]["status"] == "Flagged"
    failed = _failed_rules(env, doc)
    assert "R6" in failed and failed["R6"]["severity"] == "flag"
    detail = env.client.get(f"/api/v1/documents/{doc}", headers=env.dev(CLERK)).json()
    assert detail["headline"] == EMAIL_EXPECTED["expected"]["headline"]
    assert detail["approvals_required"] == {"count": 2, "callback": True}
    assert "000555077001" not in json.dumps(detail) and unmasked_numbers(detail) == []


# ---------------------------------------------------------------------------- R6 holds
def test_r6_cannot_be_cleared_by_agent_mcp_llm_or_a_single_human(world) -> None:
    env = _env(world)
    doc = world["email"]
    svc = env.rt.services_for(doc)
    # the agent facade (what the MCP server calls) refuses to post a Flagged document
    r = env.client.post(
        f"/api/v1/agent/documents/{doc}/post-bill",
        json={"confirmation": {"vendor_name": "x", "total_cents": 1, "confirmed_by_human": True}},
        headers=env.agent("bills:post"),
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "illegal_transition"
    # no non-human actor can vote, whatever it claims
    for actor in (Actor.mcp("claude-desktop"), Actor.system("pipeline"), Actor(kind="llm", id="x")):
        with pytest.raises(IllegalTransition):
            svc.record_approval(
                doc, kind="bank_change", approver=actor, decision="approve", callback_attested=True
            )
    url = f"/api/v1/documents/{doc}/approvals"
    vote = {"kind": "bank_change", "decision": "approve", "callback_attested": True}
    # clerks cannot vote; an approver without the callback attestation cannot either
    assert env.client.post(url, json=vote, headers=env.dev(CLERK)).status_code == 403
    no_cb = env.client.post(url, json={**vote, "callback_attested": False}, headers=env.dev(OWNER))
    assert no_cb.status_code == 409
    # one human approval is not enough
    one = env.client.post(url, json=vote, headers=env.dev(APPROVER))
    assert one.status_code == 200 and one.json()["status"] == "Flagged"
    assert env.status(doc) == "Flagged"


def test_r6_cannot_be_cleared_by_the_uploader(world) -> None:
    """A staff member who forwards the e-mail is its uploader and can never be one of the two
    approvers. (E-mail ingest has no staff uploader, so the forwarder is recorded directly.)"""
    env = _env(world)
    doc = world["email"]
    with env.db.tx() as conn:
        conn.execute("update documents set uploaded_by = %s where id = %s", (env.staff[OWNER], doc))
    url = f"/api/v1/documents/{doc}/approvals"
    vote = {"kind": "bank_change", "decision": "approve", "callback_attested": True}
    own = env.client.post(url, json=vote, headers=env.dev(OWNER))
    assert own.status_code == 403 and own.json()["error"]["code"] == "self_approval"
    # APPROVER voted already; the only other approver is the uploader, so it stays held
    assert env.status(doc) == "Flagged"
    assert env.client.post(f"/api/v1/documents/{doc}/post", headers=env.dev(CLERK)).status_code == (
        409
    )


# ---------------------------------------------------------------------------- comparison
def test_comparison_mode_naive_pays_the_duplicate_and_doctored_invoices(world) -> None:
    env = _env(world)
    comp = env.client.get(f"/api/v1/batches/{world['batch_id']}/comparison").json()
    rows = {r["filename"]: r for r in comp["rows"]}
    assert len(rows) == 25
    for prefix in ("21-rapid-rooter", "18-ember"):  # duplicate; white-text bank change
        row = next(r for n, r in rows.items() if n.startswith(prefix))
        assert row["naive"]["outcome"] == "posted", prefix
        assert row["naive"]["verdict"] == "wrong", prefix
        assert row["invoicedesk"]["verdict"] == "correct", prefix
    assert comp["summary"]["invoicedesk_wrong"] == 0
    assert comp["summary"]["naive_dollars_lost_cents"] >= 124000


# ---------------------------------------------------------------------------- posting
def test_posting_is_idempotent(world) -> None:
    env = _env(world)
    bid = world["batch_id"]
    ready = [
        str(r["id"])
        for r in env.sql(
            "select id from documents where batch_id = %s and status = 'ReadyToPay'", (bid,)
        )
    ]
    assert len(ready) == 18
    # a double-click: two post-ready requests queue before any job runs
    first = env.client.post(f"/api/v1/batches/{bid}/post-ready", headers=env.dev(CLERK)).json()
    env.client.post(f"/api/v1/batches/{bid}/post-ready", headers=env.dev(CLERK))
    assert first["count"] == 18
    env.drain()
    view = env.client.get(f"/api/v1/batches/{bid}", headers=env.dev(CLERK)).json()
    assert view["counts"]["posted"] == 18
    assert len(env.accounting.bills) == 18  # exactly one bill per document
    # re-posting a posted document (a retried job) returns the same bill, never a second one
    svc = env.rt.services_for(ready[0])
    again = svc.post_document(ready[0], actor=Actor.user(env.staff[CLERK]))
    assert again.already_posted and again.status == "Posted"
    assert (
        env.sql("select count(*) as n from bills where document_id = %s", (ready[0],))[0]["n"] == 1
    )
    assert len(env.accounting.bills) == 18
    held = env.sql(
        "select count(*) as n from bills b join documents d on d.id = b.document_id"
        " where d.status <> 'Posted'"
    )
    assert held[0]["n"] == 0  # nothing held, flagged or quarantined was ever posted


def test_audit_chain_verifies(world) -> None:
    env = _env(world)
    with env.db.tx() as conn:
        assert verify(conn) == (True, None)
        n = conn.execute("select count(*) as n from audit_log").fetchone()
    assert n is not None and n["n"] > 26 * 5


# ---------------------------------------------------------------------------- worker
@pytest.mark.usefixtures("e2e_db")
def test_worker_offline_composition_processes_a_queued_invoice(pg_db: Database, tmp_path) -> None:
    """The ``invoicedesk-worker`` offline wiring (replay LLM with the oracle as default fallback,
    local storage, fake accounting) takes an uploaded invoice from the queue to ReadyToPay and
    posts it once."""
    from invoicedesk import wiring
    from invoicedesk.db import repo
    from invoicedesk.db.queue import PostgresJobQueue
    from invoicedesk.fakes import FakeAccounting
    from invoicedesk.pipeline.deps import build_deps
    from invoicedesk.pipeline.graph import PipelineRunner, postgres_checkpointer
    from invoicedesk.pipeline.services import PipelineServices
    from invoicedesk_api.testkit import demo_file, make_settings
    from invoicedesk_data.demo import DEMO_TODAY
    from invoicedesk_data.seed import seed_database
    from invoicedesk_worker.main import Worker, build_handlers, build_llm

    settings = make_settings(pg_db.dsn, tmp_path / "storage")
    seed_database(pg_db, "maple_row")
    llm = build_llm(settings)  # offline: ReplayLLM -> OracleLLM fallback, no paid API
    accounting = FakeAccounting()
    assert isinstance(wiring.accounting_adapter(settings), FakeAccounting)
    with postgres_checkpointer(pg_db.dsn, max_size=4) as saver:
        deps = build_deps(
            pg_db, llm=llm, settings=settings, accounting=accounting, today=lambda: DEMO_TODAY
        )
        queue = PostgresJobQueue(pg_db)
        services = PipelineServices(deps, runner=PipelineRunner(deps, saver), queue=queue)
        f = demo_file("14-tumbler")
        key = f"maple_row/originals/{uuid.uuid4().hex}.pdf"
        deps.storage.put(key, f.data, content_type=f.mime)
        with pg_db.tx() as conn:
            doc_id = repo.insert_document(
                conn,
                tenant_id="maple_row",
                source="upload",
                sha256=hashlib.sha256(f.data).hexdigest(),
                storage_path=key,
                mime=f.mime,
                filename=f.filename,
                actor=Actor.system("e2e"),
            )
        services.enqueue_document(doc_id)
        worker = Worker(queue, build_handlers(services), concurrency=1)
        assert worker.run_until_idle() == 1
        with pg_db.tx() as conn:
            status = conn.execute(
                "select status::text as s from documents where id = %s", (doc_id,)
            ).fetchone()
        assert status is not None and status["s"] == "ReadyToPay"
        services.post_document(doc_id, actor=Actor.system("e2e"))
        assert len(accounting.bills) == 1
        with pg_db.tx() as conn:
            assert verify(conn) == (True, None)
