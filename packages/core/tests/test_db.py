"""Postgres-backed tests: migrations, audit chain + append-only trigger, approvals trigger,
status transitions via the repo, job queue, tenant data loading, spend ledger."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import psycopg
import pytest
from invoicedesk.audit.chain import append, verify
from invoicedesk.contracts.extractor import FieldResult, TierRun
from invoicedesk.contracts.llm import Usage
from invoicedesk.contracts.rules import RuleResult
from invoicedesk.contracts.screening import ScreenResult, ScreenSpan
from invoicedesk.db import Database, repo
from invoicedesk.db.queue import PostgresJobQueue
from invoicedesk.remit import remit_fingerprint
from invoicedesk.status import (
    ALL_RULE_IDS,
    Actor,
    ApprovalEvidence,
    ApprovalVote,
    DocStatus,
    IllegalTransition,
    PostingReceipt,
    RuleOutcome,
    route,
)
from invoicedesk.testing.builders import make_extraction

pytestmark = pytest.mark.postgres
S = DocStatus
SYS = Actor.system()


def _seed(db: Database) -> dict[str, str]:
    with db.tx() as c:
        repo.ensure_tenant(c, "maple_row", "Maple Row Property Management")
        uploader = repo.create_staff_user(
            c, tenant_id="maple_row", email="clerk@example.test", display_name="Clerk"
        )
        a1 = repo.create_staff_user(
            c, tenant_id="maple_row", email="a1@example.test", display_name="A1", role="approver"
        )
        a2 = repo.create_staff_user(
            c, tenant_id="maple_row", email="a2@example.test", display_name="A2", role="owner"
        )
        prop = c.execute(
            "insert into properties (tenant_id, name, address) values ('maple_row','Alder Court','1 Alder Ct') returning id"
        ).fetchone()
        assert prop
        unit = c.execute(
            "insert into units (property_id, label) values (%s, '12B') returning id", (prop["id"],)
        ).fetchone()
        assert unit
        v = c.execute(
            "insert into vendors (tenant_id, legal_name, aliases, tax_id_last4, remit_to_hash, bank_last4)"
            " values ('maple_row','Rapid Rooter Plumbing LLC', '{Rapid Rooter}', '4821', %s, '4567') returning id",
            (remit_fingerprint("Routing 021000021 Acct 0001234567"),),
        ).fetchone()
        assert v
        wo = c.execute(
            "insert into work_orders (tenant_id, wo_number, unit_id, vendor_id, scope, approved_estimate_cents)"
            " values ('maple_row','WO-1187',%s,%s,'Kitchen drain',100000) returning id",
            (unit["id"], v["id"]),
        ).fetchone()
        assert wo
        c.execute(
            "insert into estimate_lines (work_order_id, description, amount_cents) values (%s,'Drain trap',100000)",
            (wo["id"],),
        )
        c.execute(
            "insert into historical_bills (tenant_id, vendor_id, invoice_number, invoice_date, total_cents)"
            " values ('maple_row',%s,'INV-2231','2026-10-08',124000)",
            (v["id"],),
        )
        doc = repo.insert_document(
            c,
            tenant_id="maple_row",
            source="upload",
            sha256="ab" * 32,
            storage_path="maple_row/originals/x/a.pdf",
            mime="application/pdf",
            uploaded_by=uploader,
            filename="a.pdf",
        )
    return {"uploader": uploader, "a1": a1, "a2": a2, "doc": doc, "vendor": str(v["id"])}


def test_migrations_applied(pg_db: Database) -> None:
    with pg_db.tx() as c:
        names = {r["filename"] for r in c.execute("select filename from schema_migrations")}
        assert "0001_init.sql" in names
        tables = {
            r["tablename"]
            for r in c.execute("select tablename from pg_tables where schemaname='public'")
        }
    for t in ("tenants", "documents", "audit_log", "bills", "jobs", "eval_results", "spend_ledger"):
        assert t in tables


def test_audit_chain_and_append_only(pg_db: Database) -> None:
    ids = _seed(pg_db)
    with pg_db.tx() as c:
        e = append(
            c,
            actor="user:x",
            action="test",
            document_id=ids["doc"],
            tenant_id="maple_row",
            after={"bank_account": "000123456789", "amount_cents": 5, "cost": Decimal("0.1")},
        )
        assert e.after["bank_account"] == "••••••••6789"
        ok, bad = verify(c)
        assert ok and bad is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pg_db.tx() as c:
        c.execute("update audit_log set action = 'x' where id = %s", (e.id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pg_db.tx() as c:
        c.execute("delete from audit_log")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pg_db.tx() as c:
        c.execute("truncate audit_log")
    # tamper detection: bypass the trigger as superuser and check verify catches it
    with pg_db.tx() as c:
        c.execute("alter table audit_log disable trigger audit_log_no_update")
        c.execute("update audit_log set action = 'forged' where id = %s", (e.id,))
        c.execute("alter table audit_log enable trigger audit_log_no_update")
        ok, bad = verify(c)
        assert not ok and bad == e.id


def test_status_flow_through_repo(pg_db: Database) -> None:
    ids = _seed(pg_db)
    doc = ids["doc"]
    with pg_db.tx() as c:
        for t in (S.SCREENING, S.EXTRACTING, S.EXTRACTED, S.VALIDATING):
            repo.set_status(c, doc, t, actor=SYS, reason="pipeline")
        outcomes = [
            RuleOutcome(r, r != "R6", "pass" if r != "R6" else "flag", "bank change")
            for r in ALL_RULE_IDS
        ]
        repo.set_status(c, doc, S.FLAGGED, actor=SYS, reason="rules", evidence=route(outcomes))
    with pytest.raises(IllegalTransition), pg_db.tx() as c:
        repo.set_status(c, doc, S.READY_TO_PAY, actor=Actor.user(ids["a1"]), reason="x")
    # uploader cannot approve (DB trigger)
    with pytest.raises(psycopg.errors.CheckViolation), pg_db.tx() as c:
        repo.record_approval(
            c,
            doc,
            kind="bank_change",
            approver_id=ids["uploader"],
            decision="approve",
            callback_attested=True,
        )
    # bank_change approval without callback is rejected by the DB
    with pytest.raises(psycopg.errors.CheckViolation), pg_db.tx() as c:
        repo.record_approval(
            c,
            doc,
            kind="bank_change",
            approver_id=ids["a1"],
            decision="approve",
            callback_attested=False,
        )
    with pg_db.tx() as c:
        repo.record_approval(
            c,
            doc,
            kind="bank_change",
            approver_id=ids["a1"],
            decision="approve",
            callback_attested=True,
        )
        repo.record_approval(
            c,
            doc,
            kind="bank_change",
            approver_id=ids["a2"],
            decision="approve",
            callback_attested=True,
        )
        votes = tuple(
            ApprovalVote(a.approver_id, a.decision, a.callback_attested, a.kind)
            for a in repo.list_approvals(c, doc)
        )
        repo.set_status(
            c,
            doc,
            S.READY_TO_PAY,
            actor=Actor.user(ids["a2"]),
            reason="approved",
            evidence=ApprovalEvidence(
                votes,
                tuple(
                    RuleOutcome(r, r != "R6", "flag" if r == "R6" else "pass") for r in ALL_RULE_IDS
                ),
            ),
        )
        key = f"invoicedesk-bill-{doc}"
        repo.record_bill(c, doc, system="fake", idempotency_key=key, status="pending")
        repo.record_bill(
            c, doc, system="fake", idempotency_key=key, status="posted", external_id="b1"
        )
        repo.set_status(
            c, doc, S.POSTED, actor=SYS, reason="posted", evidence=PostingReceipt("fake", "b1", key)
        )
        assert repo.get_document(c, doc).status is S.POSTED
        n_bills = c.execute(
            "select count(*) as n from bills where document_id = %s", (doc,)
        ).fetchone()
        assert n_bills and n_bills["n"] == 1
        actions = [r["action"] for r in c.execute("select action from audit_log order by id")]
        assert actions[0] == "document.created"
        assert actions.count("status.changed") == 7
        assert "approval.approve" in actions and "bill.posted" in actions
        assert verify(c)[0]
        # notify trigger fired without error; facts
        assert repo.document_facts(repo.get_document(c, doc)).uploaded_by == ids["uploader"]
    with pytest.raises(KeyError), pg_db.tx() as c:
        repo.get_document(c, "00000000-0000-0000-0000-000000000000")


def test_records_and_tenant_data(pg_db: Database) -> None:
    ids = _seed(pg_db)
    doc = ids["doc"]
    with pg_db.tx() as c:
        run = TierRun(
            tier="haiku",
            model_id="claude-haiku-5-5",
            output=make_extraction(),
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd="0.00090000",
            latency_ms=12,
        )
        repo.record_extraction(c, doc, run)
        repo.record_extraction(c, doc, run.model_copy(update={"output": None, "refused": True}))
        repo.save_fields(
            c,
            doc,
            [
                FieldResult(
                    name="total",
                    value="124000",
                    raw_conf=0.9,
                    calibrated_conf=0.8,
                    word_ids=[1, 2],
                    box=(1, 2, 3, 4),
                ),
                FieldResult(name="vendor_id", value=ids["vendor"], raw_conf=1, calibrated_conf=1),
                FieldResult(name="invoice_number", value="INV-9", raw_conf=1, calibrated_conf=1),
                FieldResult(name="invoice_date", value="2026-10-10", raw_conf=1, calibrated_conf=1),
            ],
        )
        repo.save_fields(
            c, doc, [FieldResult(name="total", value="124500", raw_conf=0.9, calibrated_conf=0.99)]
        )
        finals = c.execute(
            "select value from fields where document_id=%s and name='total' and final", (doc,)
        ).fetchall()
        assert [r["value"] for r in finals] == ["124500"]
        repo.record_screen_results(
            c,
            doc,
            "maple_row",
            [
                ScreenResult(
                    detector="hidden_text",
                    verdict="suspicious",
                    spans=[ScreenSpan(text="wire to acct 9876543210", reason="white text")],
                )
            ],
        )
        spans = c.execute(
            "select spans_json from screen_results where document_id=%s", (doc,)
        ).fetchone()
        assert spans and "9876543210" not in str(spans["spans_json"])
        rr = [
            RuleResult(rule_id=r, name=r, passed=True, severity="pass", message="ok")
            for r in ALL_RULE_IDS
        ]
        assert repo.record_rule_results(c, doc, "maple_row", rr) == 1
        assert repo.record_rule_results(c, doc, "maple_row", rr) == 2
        repo.record_review(
            c,
            doc,
            field_name="total",
            old_value="124000",
            new_value="124500",
            reviewer_id=ids["a1"],
        )
        cost = repo.get_document(c, doc).total_cost_usd
        assert cost == Decimal("0.0018")

        td = repo.load_tenant_data(c, "maple_row")
        assert td.vendors[0].bank_last4 == "4567" and td.vendors[0].aliases == ("Rapid Rooter",)
        wo = td.work_order("WO-1187")
        assert wo and wo.estimate_lines[0].amount_cents == 100_000
        assert td.vendor(ids["vendor"]) and td.unit(wo.unit_id)
        assert (
            td.work_order("nope") is None and td.vendor("nope") is None and td.unit("nope") is None
        )
        kinds = sorted(p.source for p in td.prior_invoices)
        assert kinds == ["document", "history"]
        d = next(p for p in td.prior_invoices if p.source == "document")
        assert (d.total_cents, d.invoice_number, d.invoice_date) == (
            124_500,
            "INV-9",
            dt.date(2026, 10, 10),
        )
        td2 = repo.load_tenant_data(c, "maple_row", exclude_document_id=doc)
        assert [p.source for p in td2.prior_invoices] == ["history"]


def test_spend_ledger_and_batches(pg_db: Database) -> None:
    _seed(pg_db)
    with pg_db.tx() as c:
        b = repo.create_batch(c, tenant_id="maple_row", label="demo", mode="demo")
        assert b
        assert repo.add_spend(c, scope="global", key="all", usd=Decimal("1.25")) == Decimal("1.25")
        assert repo.add_spend(c, scope="global", key="all", usd=Decimal("0.50")) == Decimal("1.75")
        assert repo.get_spend(c, scope="global", key="all") == Decimal("1.75")
        assert repo.get_spend(c, scope="ip", key="1.2.3.4") == Decimal(0)
        # visitor docs must carry purge_after
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            repo.insert_document(
                c,
                tenant_id="maple_row",
                source="upload",
                sha256="cd" * 32,
                storage_path="visitor/s/a.png",
                mime="image/png",
                session_id="s",
            )


def test_postgres_queue(pg_db: Database) -> None:
    ids = _seed(pg_db)
    q = PostgresJobQueue(pg_db, base_backoff_s=0)
    j = q.enqueue("process_document", document_id=ids["doc"], dedupe_key="doc:" + ids["doc"])
    assert (
        q.enqueue("process_document", document_id=ids["doc"], dedupe_key="doc:" + ids["doc"]) == j
    )
    q.enqueue("send_followup", payload={"x": 1}, max_attempts=1)
    assert q.claim("w1", kinds=["nope"]) is None
    job = q.claim("w1", kinds=["process_document"])
    assert job and job.id == j and job.attempts == 1 and job.document_id == ids["doc"]
    assert q.claim("w2", kinds=["process_document"]) is None  # locked / running
    q.fail(job.id, "transient", retry_in_s=0)
    job2 = q.claim("w2", kinds=["process_document"])
    assert job2 and job2.attempts == 2
    q.complete(job2.id)
    other = q.claim("w1")
    assert other and other.payload == {"x": 1}
    q.fail(other.id, "fatal")
    assert q.claim("w1") is None
    with pg_db.tx() as c:
        st = {r["kind"]: r["status"] for r in c.execute("select kind, status from jobs")}
    assert st == {"process_document": "done", "send_followup": "failed"}
    j3 = q.enqueue("post_bill")
    assert q.claim("w9") is not None
    with pg_db.tx() as c:
        c.execute("update jobs set locked_at = now() - interval '1 hour' where id = %s", (j3,))
    assert q.requeue_stale(older_than_s=60) == 1
