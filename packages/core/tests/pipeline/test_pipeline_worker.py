"""The worker on the Postgres SKIP LOCKED queue: process, post, resume, stale requeue,
follow-ups."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from invoicedesk.db.queue import PostgresJobQueue
from invoicedesk.pipeline.graph import PipelineRunner
from invoicedesk.pipeline.services import PipelineServices
from invoicedesk.testing.builders import make_extraction, money, text
from invoicedesk_worker.main import Worker, build_handlers, send_followup

pytestmark = pytest.mark.postgres


def _job(world: Any, job_id: int) -> dict[str, Any]:
    with world.db.tx() as conn:
        row = conn.execute("select * from jobs where id = %s", (job_id,)).fetchone()
    assert row is not None
    return dict(row)


@pytest.fixture
def queue(world: Any) -> PostgresJobQueue:
    return PostgresJobQueue(world.db, base_backoff_s=0.0)


@pytest.fixture
def qservices(world: Any, runner: PipelineRunner, queue: PostgresJobQueue) -> PipelineServices:
    return PipelineServices(world.deps, runner=runner, queue=queue)


def test_worker_processes_and_posts_via_queue(
    world: Any, queue: PostgresJobQueue, qservices: PipelineServices
) -> None:
    batch = world.batch()
    doc = world.add_document(make_extraction(invoice_number=text("INV-7001")), batch_id=batch)
    jid = qservices.enqueue_document(doc)
    assert qservices.enqueue_document(doc) == jid  # deduped while queued
    worker = Worker(queue, build_handlers(qservices), poll_interval_s=0.01)
    assert worker.run_until_idle() == 1
    assert world.status(doc) == "ReadyToPay"
    assert _job(world, jid)["status"] == "done"

    job_ids = qservices.post_ready(batch)
    assert len(job_ids) == 1
    worker.run_until_idle()
    assert world.status(doc) == "Posted"
    assert len(world.accounting.bills) == 1
    # A second post job for a Posted document is a no-op success (idempotent).
    again = qservices.enqueue_post(doc)
    worker.run_until_idle()
    assert _job(world, again)["status"] == "done"
    assert len(world.accounting.bills) == 1
    assert world.audit_ok()


def test_killed_worker_job_is_requeued_and_resumed(
    world: Any, queue: PostgresJobQueue, qservices: PipelineServices
) -> None:
    doc = world.add_document(make_extraction(invoice_number=text("INV-7002")))
    jid = qservices.enqueue_document(doc)
    dead = queue.claim("dead-worker")  # claimed, then the worker vanished
    assert dead is not None and dead.id == jid
    worker = Worker(queue, build_handlers(qservices), stale_after_s=0, poll_interval_s=0.01)
    assert worker.run_until_idle() == 0  # still locked by the dead worker
    assert worker.requeue_stale() == 1
    assert worker.run_until_idle() == 1
    assert world.status(doc) == "ReadyToPay"
    assert _job(world, jid)["attempts"] == 2


def test_posting_a_flagged_document_fails_permanently(
    world: Any, runner: PipelineRunner, queue: PostgresJobQueue, qservices: PipelineServices
) -> None:
    doc = world.add_document(make_extraction())  # duplicate -> Flagged
    runner.process(doc)
    jid = qservices.enqueue_post(doc)
    Worker(queue, build_handlers(qservices)).run_until_idle()
    job = _job(world, jid)
    assert job["status"] == "failed" and job["attempts"] == 1
    assert "IllegalTransition" in job["last_error"]
    assert world.accounting.bills == {}


def test_resume_job_after_correction_without_inline_runner(
    world: Any, runner: PipelineRunner, queue: PostgresJobQueue
) -> None:
    """The API can record a correction and leave re-validation to the worker."""
    api = PipelineServices(world.deps, queue=queue)  # no runner: resumes are queued
    doc = world.add_document(
        make_extraction(invoice_number=text("INV-7003"), total=money(124_000, conf=0.2))
    )
    assert runner.process(doc)["status"] == "NeedsReview"
    out = api.apply_corrections(doc, [{"field": "total", "value": "$1,240.00"}], world.clerk)
    assert out.status == "Validating"
    worker = Worker(queue, build_handlers(PipelineServices(world.deps, runner=runner, queue=queue)))
    assert worker.run_until_idle() == 1
    assert world.status(doc) == "ReadyToPay"


def _followup(world: Any, doc: str, *, approved: bool) -> str:
    with world.db.tx() as conn:
        row = conn.execute(
            "insert into followups (document_id, to_addr, draft_subject, draft_body,"
            " approved_by, approved_at) values (%s, 'billing@rapidrooter.test', 'Missing WO',"
            " 'Please resend with the work-order number. Acct 000123456789', %s,"
            " case when %s then now() end) returning id",
            (doc, world.approver_a if approved else None, approved),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def test_send_followup_posts_to_n8n_once(world: Any) -> None:
    doc = world.add_document(make_extraction(invoice_number=text("INV-7004")))
    fid = _followup(world, doc, approved=True)
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert send_followup(world.db, fid, webhook_url="http://n8n.test/webhook/send", http=http)
    assert not send_followup(world.db, fid, webhook_url="http://n8n.test/webhook/send", http=http)
    assert len(seen) == 1
    assert seen[0]["to"] == "billing@rapidrooter.test"
    assert "000123456789" not in seen[0]["body"]
    with world.db.tx() as conn:
        row = conn.execute("select sent_at from followups where id = %s", (fid,)).fetchone()
        audit = conn.execute(
            "select count(*) as n from audit_log where action = 'followup.sent'"
        ).fetchone()
    assert row is not None and row["sent_at"] is not None
    assert audit is not None and audit["n"] == 1
    assert world.audit_ok()


def test_send_followup_is_noop_offline_or_unapproved(
    world: Any, queue: PostgresJobQueue, qservices: PipelineServices
) -> None:
    doc = world.add_document(make_extraction(invoice_number=text("INV-7005")))
    approved = _followup(world, doc, approved=True)
    draft = _followup(world, doc, approved=False)
    assert not send_followup(world.db, approved, webhook_url=None)
    assert not send_followup(world.db, draft, webhook_url="http://n8n.test/x")
    jid = queue.enqueue("send_followup", payload={"followup_id": approved})
    Worker(queue, build_handlers(qservices, followup_webhook_url=None)).run_until_idle()
    assert _job(world, jid)["status"] == "done"

    def refuse(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    bad = queue.enqueue("send_followup", payload={"followup_id": approved})
    http = httpx.Client(transport=httpx.MockTransport(refuse))
    Worker(
        queue, build_handlers(qservices, followup_webhook_url="http://n8n.test/x", http=http)
    ).run_until_idle()
    assert _job(world, bad)["status"] == "failed"


def test_eight_threads_drain_a_batch(
    world: Any, queue: PostgresJobQueue, qservices: PipelineServices
) -> None:
    import time

    docs = [
        world.add_document(make_extraction(invoice_number=text(f"INV-71{i:02d}")))
        for i in range(10)
    ]
    for d in docs:
        qservices.enqueue_document(d)
    worker = Worker(queue, build_handlers(qservices), concurrency=8, poll_interval_s=0.02)
    worker.start()
    deadline = time.monotonic() + 120
    terminal = {"ReadyToPay", "NeedsReview", "Flagged"}
    while time.monotonic() < deadline and not all(world.status(d) in terminal for d in docs):
        time.sleep(0.1)
    worker.stop(timeout=30)
    assert all(world.status(d) in terminal for d in docs)
    with world.db.tx() as conn:
        row = conn.execute("select count(*) as n from jobs where status <> 'done'").fetchone()
    assert row is not None and row["n"] == 0
    assert all(world.count("extractions", d) == 1 for d in docs)
    assert world.audit_ok()


def test_worker_main_once_drains_queue(world: Any, pg_dsn: str, monkeypatch: Any) -> None:
    from invoicedesk.config import settings as settings_mod
    from invoicedesk_worker import main as worker_main

    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("STORAGE_BACKEND", "memory")
    settings_mod.get_settings.cache_clear()
    try:
        assert worker_main.main(["--once", "--dsn", pg_dsn], llm=world.llm) == 0
    finally:
        settings_mod.get_settings.cache_clear()
