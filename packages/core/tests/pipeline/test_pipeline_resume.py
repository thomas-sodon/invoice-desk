"""Crash/resume: the PostgresSaver checkpoint plus per-node step markers mean a killed worker
resumes where it stopped and never duplicates rows or model calls."""

from __future__ import annotations

from typing import Any

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.pipeline.graph import PipelineRunner, postgres_checkpointer
from invoicedesk.testing.builders import make_extraction, text

pytestmark = pytest.mark.postgres


class WorkerKilled(BaseException):
    """Stands in for SIGKILL: not an Exception, so nothing in the pipeline swallows it."""


def _clean() -> Any:
    return make_extraction(invoice_number=text("INV-4001"))


def test_kill_during_extraction_resumes_from_checkpoint(world: Any, pg_dsn: str) -> None:
    doc = world.add_document(_clean())
    real = world.deps.extractor
    calls = {"n": 0}

    class DiesOnce:
        def extract(self, *a: Any, **kw: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise WorkerKilled
            return real.extract(*a, **kw)

    world.deps.extractor = DiesOnce()
    with postgres_checkpointer(pg_dsn) as saver:
        with pytest.raises(WorkerKilled):
            PipelineRunner(world.deps, saver).process(doc)
    assert world.status(doc) == "Extracting"

    # A fresh process: new checkpointer connection, new compiled graph.
    with postgres_checkpointer(pg_dsn) as saver:
        res = PipelineRunner(world.deps, saver).process(doc)

    assert res["status"] == "ReadyToPay"
    assert world.preprocessor.calls == [doc]  # preprocess not re-run
    assert len(world.calls(doc, {ModelRole.INJECTION_CLASSIFIER})) == 1  # screening not re-run
    assert world.count("screen_results", doc) == 3
    assert world.count("extractions", doc) == 1
    assert world.statuses(doc).count("Extracting") == 1
    assert world.audit_ok()


def test_kill_after_commit_before_checkpoint_does_not_duplicate(world: Any, pg_dsn: str) -> None:
    """The extract node committed its rows, then the process died before LangGraph saved the
    checkpoint: the re-run node finds its step marker and writes nothing new."""
    doc = world.add_document(_clean())

    class DiesOnExtracted:
        armed = True

        def publish(self, event: BoardEvent) -> None:
            if self.armed and event.status == "Extracted":
                self.armed = False
                raise WorkerKilled

    world.deps.publisher = DiesOnExtracted()
    with postgres_checkpointer(pg_dsn) as saver:
        with pytest.raises(WorkerKilled):
            PipelineRunner(world.deps, saver).process(doc)
    assert world.status(doc) == "Extracted"

    with postgres_checkpointer(pg_dsn) as saver:
        res = PipelineRunner(world.deps, saver).process(doc)

    assert res["status"] == "ReadyToPay"
    assert len(world.extraction_calls(doc)) == 1  # no second model call
    assert world.count("extractions", doc) == 1
    with world.db.tx() as conn:
        n = conn.execute(
            "select count(*) as n from fields where document_id = %s and name = 'total'", (doc,)
        ).fetchone()
    assert n is not None and n["n"] == 1
    assert world.audit_ok()


def test_process_is_idempotent_and_waits_for_humans(world: Any, runner: PipelineRunner) -> None:
    doc = world.add_document(make_extraction())  # duplicate of INV-2231 -> Flagged
    first = runner.process(doc)
    again = runner.process(doc)
    assert first == again
    assert first["waiting_for_human"]
    assert world.count("rule_results", doc) == 10
    assert runner.state(doc).get("route") == "Flagged"


def test_resume_of_finished_thread_routes_from_db(
    world: Any, runner: PipelineRunner, services: Any
) -> None:
    doc = world.add_document(_clean())
    assert runner.process(doc)["status"] == "ReadyToPay"
    res = runner.resume(doc, {"action": "noop"})  # nothing to do: ends again
    assert res["status"] == "ReadyToPay" and not res["waiting_for_human"]
    with pytest.raises(LookupError):
        runner.resume("00000000-0000-0000-0000-000000000000")


def test_delete_thread_drops_checkpoints(world: Any, runner: PipelineRunner) -> None:
    doc = world.add_document(_clean(), session_id="sess-x", uploaded_by=None)
    runner.process(doc)
    assert runner.state(doc)
    runner.delete_thread(doc)
    assert not runner.state(doc)


def test_concurrent_copies_of_one_invoice_are_caught(world: Any, runner: PipelineRunner) -> None:
    import threading

    a = world.add_document(make_extraction(invoice_number=text("INV-9001")))
    b = world.add_document(make_extraction(invoice_number=text("INV-9001")))
    barrier = threading.Barrier(2)
    real = world.deps.extractor

    class Together:
        def extract(self, *args: Any, **kw: Any) -> Any:
            out = real.extract(*args, **kw)
            barrier.wait(timeout=30)  # both reach validate at the same moment
            return out

    world.deps.extractor = Together()
    errors: list[BaseException] = []

    def run(doc: str) -> None:
        try:
            runner.process(doc)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(d,)) for d in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    statuses = {world.status(a), world.status(b)}
    assert "Flagged" in statuses  # at least the second copy is a duplicate
    flagged = a if world.status(a) == "Flagged" else b
    assert "R4" in world.failed_rules(flagged)


def test_validation_waits_for_the_tenant_validation_lock(
    world: Any, runner: PipelineRunner, pg_dsn: str
) -> None:
    """Proves the serialization the R4 race relies on: while another session holds the
    tenant's validation lock, validate cannot evaluate rules (the timing test above can pass
    by luck)."""
    import threading

    import psycopg

    doc = world.add_document(make_extraction(invoice_number=text("INV-9101")))
    done = threading.Event()

    def run() -> None:
        runner.process(doc)
        done.set()

    with psycopg.connect(pg_dsn, autocommit=True) as holder:
        holder.execute("select pg_advisory_lock(hashtext(%s))", ("invoicedesk.validate:maple_row",))
        t = threading.Thread(target=run)
        t.start()
        try:
            assert not done.wait(2.0)  # blocked on the lock
            assert world.count("rule_results", doc) == 0
        finally:
            holder.execute(
                "select pg_advisory_unlock(hashtext(%s))", ("invoicedesk.validate:maple_row",)
            )
        t.join(60)
    assert done.is_set()
    assert world.status(doc) == "ReadyToPay"
