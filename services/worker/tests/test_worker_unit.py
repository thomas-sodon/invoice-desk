"""Worker loop semantics on the in-memory queue (no Postgres needed)."""

from __future__ import annotations

import threading
import time

from invoicedesk.contracts.accounting import AccountingError
from invoicedesk.contracts.infra import Job
from invoicedesk.fakes import InMemoryJobQueue
from invoicedesk.status import IllegalTransition
from invoicedesk_worker.main import JOB_KINDS, NonRetryable, Worker, is_permanent, parse_actor


def _worker(q: InMemoryJobQueue, handlers: dict, **kw: object) -> Worker:
    return Worker(q, handlers, poll_interval_s=0.01, **kw)  # type: ignore[arg-type]


def test_success_completes_job() -> None:
    q = InMemoryJobQueue()
    seen: list[str | None] = []
    jid = q.enqueue("process_document", document_id="d1")
    w = _worker(q, {"process_document": lambda j: seen.append(j.document_id)})
    assert w.run_until_idle() == 1
    assert seen == ["d1"]
    assert q.jobs[jid]["status"] == "done"


def test_transient_error_retries_with_backoff_then_succeeds() -> None:
    q = InMemoryJobQueue()
    calls = {"n": 0}

    def flaky(job: Job) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("db hiccup 000123456789")

    jid = q.enqueue("process_document", document_id="d1", max_attempts=5)
    w = _worker(q, {"process_document": flaky}, base_backoff_s=0.0)
    for _ in range(5):
        w.run_until_idle()
    assert calls["n"] == 3
    assert q.jobs[jid]["status"] == "done"
    assert "000123456789" not in (q.jobs[jid]["last_error"] or "")  # masked error text


def test_backoff_is_exponential_and_capped() -> None:
    w = _worker(InMemoryJobQueue(), {}, base_backoff_s=2.0, max_backoff_s=30.0)
    assert [w.backoff(a) for a in (1, 2, 3, 4, 5, 9)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_permanent_errors_fail_immediately() -> None:
    q = InMemoryJobQueue()

    def illegal(job: Job) -> None:
        raise IllegalTransition("Flagged -> Posted")

    jid = q.enqueue("post_bill", document_id="d1", max_attempts=5)
    w = _worker(q, {"post_bill": illegal})
    w.run_until_idle()
    assert q.jobs[jid]["status"] == "failed"
    assert q.jobs[jid]["attempts"] == 1


def test_exhausted_attempts_fail() -> None:
    q = InMemoryJobQueue()

    def boom(job: Job) -> None:
        raise RuntimeError("still down")

    jid = q.enqueue("process_document", document_id="d1", max_attempts=2)
    w = _worker(q, {"process_document": boom}, base_backoff_s=0.0)
    for _ in range(3):
        w.run_until_idle()
    assert q.jobs[jid]["status"] == "failed"
    assert q.jobs[jid]["attempts"] == 2


def test_unknown_kind_is_not_claimed_and_missing_handler_fails() -> None:
    q = InMemoryJobQueue()
    other = q.enqueue("something_else")
    w = _worker(q, {"process_document": lambda j: None})
    assert w.run_until_idle() == 0
    assert q.jobs[other]["status"] == "queued"
    job = Job(id=other, kind="nope", document_id=None, payload={}, attempts=1, max_attempts=5)
    q.jobs[other]["status"] = "running"
    q.jobs[other]["attempts"] = 1
    assert w.handle(job) is False
    assert q.jobs[other]["status"] == "failed"


def test_is_permanent() -> None:
    assert is_permanent(NonRetryable("x"))
    assert is_permanent(AccountingError("bad request", retryable=False))
    assert not is_permanent(AccountingError("503", retryable=True))
    assert not is_permanent(TimeoutError())


def test_parse_actor_never_yields_llm() -> None:
    assert parse_actor("user:abc").kind == "user"
    assert parse_actor("mcp:claude").audit_label == "mcp:claude"
    assert parse_actor("llm:haiku").kind == "system"
    assert parse_actor(None).kind == "system"
    assert parse_actor("system").kind == "system"


def test_threads_drain_concurrently_and_stop_gracefully() -> None:
    q = InMemoryJobQueue()
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(job: Job) -> None:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1

    ids = [q.enqueue("process_document", document_id=f"d{i}") for i in range(16)]
    w = _worker(q, {"process_document": slow}, concurrency=8)
    w.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(q.jobs[i]["status"] != "done" for i in ids):
        time.sleep(0.01)
    w.stop(timeout=5)
    assert all(q.jobs[i]["status"] == "done" for i in ids)
    assert active["max"] > 1  # ran in parallel
    assert active["now"] == 0  # nothing left half-done


def test_job_kinds() -> None:
    assert set(JOB_KINDS) == {"process_document", "resume_document", "post_bill", "send_followup"}
