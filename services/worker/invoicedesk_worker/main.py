"""``invoicedesk-worker``: the Postgres job-queue consumer that runs the LangGraph pipeline.

* Job kinds: ``process_document``, ``resume_document``, ``post_bill``, ``send_followup``.
* Concurrency 8 (threads; ``WORKER_CONCURRENCY``), each claiming with ``FOR UPDATE SKIP LOCKED``.
* Retries with exponential backoff (2 s, 4 s, 8 s ... capped); errors that a retry cannot fix
  (illegal transition, rejected input, unmappable bill, non-retryable accounting error) fail the
  job at once. Error text stored on the job is masked (invariant 9).
* On start, jobs left ``running`` by a dead worker are requeued; the LangGraph checkpoint and the
  pipeline's step markers make the re-run resume where the dead worker stopped.
* SIGTERM / SIGINT stop claiming, let in-flight jobs finish, then exit (graceful shutdown).

Offline mode never calls a paid API: the LLM is ``wiring.llm_client`` (replay), with a fallback
client named by ``INVOICEDESK_LLM_FALLBACK=module:factory``, loaded by import path so this
package does not depend on it. Unset, offline mode uses the data component's gold-backed oracle
(:data:`DEFAULT_OFFLINE_FALLBACK`) when that package is installed; ``none`` disables it (replay
misses then fail the job).
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
from invoicedesk.audit import append as audit_append
from invoicedesk.config.settings import Settings, get_settings
from invoicedesk.contracts.accounting import AccountingError
from invoicedesk.contracts.infra import Job, JobQueue
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import RejectedInput
from invoicedesk.db.connection import Database
from invoicedesk.db.queue import PostgresJobQueue
from invoicedesk.masking import mask_text
from invoicedesk.pipeline.corrections import InvalidCorrection
from invoicedesk.pipeline.posting import PostingError
from invoicedesk.pipeline.services import PipelineServices
from invoicedesk.status import Actor, IllegalTransition

__all__ = [
    "DEFAULT_OFFLINE_FALLBACK",
    "JOB_KINDS",
    "NonRetryable",
    "Worker",
    "build_handlers",
    "main",
    "parse_actor",
    "send_followup",
]

log = logging.getLogger("invoicedesk.worker")

JOB_KINDS: tuple[str, ...] = (
    "process_document",
    "resume_document",
    "post_bill",
    "send_followup",
)
FOLLOWUP_WEBHOOK_ENV = "N8N_FOLLOWUP_WEBHOOK_URL"
LLM_FALLBACK_ENV = "INVOICEDESK_LLM_FALLBACK"
#: Offline default for :data:`LLM_FALLBACK_ENV`: the gold-backed oracle (unknown documents get
#: an empty extraction, so R9 holds them for a human).
DEFAULT_OFFLINE_FALLBACK = "invoicedesk_data.oracle:OracleLLM.from_gold"

Handler = Callable[[Job], None]


class NonRetryable(Exception):
    """Fail the job now; retrying cannot help."""


_PERMANENT: tuple[type[BaseException], ...] = (
    NonRetryable,
    IllegalTransition,
    RejectedInput,
    PostingError,
    InvalidCorrection,
    LookupError,
)


def is_permanent(exc: BaseException) -> bool:
    if isinstance(exc, AccountingError):
        return not exc.retryable
    return isinstance(exc, _PERMANENT)


def parse_actor(label: object) -> Actor:
    """``audit_label`` back to an Actor ('system', 'user:<id>', 'mcp:<client>'); never llm."""
    if not isinstance(label, str) or label == "system" or ":" not in label:
        return Actor.system("posting")
    kind, _, ident = label.partition(":")
    if kind == "user":
        return Actor.user(ident)
    if kind == "mcp":
        return Actor.mcp(ident)
    return Actor.system("posting")


# ---------------------------------------------------------------------------- follow-ups
def send_followup(
    db: Database,
    followup_id: str,
    *,
    webhook_url: str | None,
    http: httpx.Client | None = None,
) -> bool:
    """POST an approved follow-up to the n8n send webhook (Mailpit in dev); mark it sent.

    No-op (returns False) without a webhook URL (offline), or when the draft is not approved
    or already sent. The recipient was taken from the vendor master when the draft was made.
    """
    with db.tx() as conn:
        row = conn.execute(
            "select f.id, f.document_id, f.to_addr, f.draft_subject, f.draft_body,"
            " f.approved_by, f.sent_at, d.tenant_id from followups f join documents d"
            " on d.id = f.document_id where f.id = %s",
            (followup_id,),
        ).fetchone()
    if row is None:
        raise NonRetryable(f"follow-up {followup_id} not found")
    if row["approved_by"] is None or row["sent_at"] is not None:
        return False
    if not webhook_url:
        log.info("follow-up %s approved; no %s set, not sending", followup_id, FOLLOWUP_WEBHOOK_ENV)
        return False
    body = {
        "followup_id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "to": row["to_addr"],
        "subject": row["draft_subject"],
        "body": mask_text(row["draft_body"]),
    }
    client = http or httpx.Client(timeout=15.0)
    try:
        resp = client.post(webhook_url, json=body)
    finally:
        if http is None:
            client.close()
    if resp.status_code >= 500 or resp.status_code == 429:
        raise RuntimeError(f"n8n webhook returned {resp.status_code}")
    if resp.status_code >= 400:
        raise NonRetryable(f"n8n webhook refused the follow-up ({resp.status_code})")
    with db.tx() as conn:
        updated = conn.execute(
            "update followups set sent_at = now() where id = %s and sent_at is null",
            (followup_id,),
        ).rowcount
        if updated:
            audit_append(
                conn,
                actor=Actor.system("worker").audit_label,
                action="followup.sent",
                document_id=str(row["document_id"]),
                tenant_id=row["tenant_id"],
                after={"followup_id": str(row["id"]), "to": row["to_addr"]},
            )
    return bool(updated)


# ---------------------------------------------------------------------------- handlers
def build_handlers(
    services: PipelineServices,
    *,
    followup_webhook_url: str | None = None,
    http: httpx.Client | None = None,
) -> dict[str, Handler]:
    def need_doc(job: Job) -> str:
        if not job.document_id:
            raise NonRetryable(f"{job.kind} job {job.id} has no document_id")
        return job.document_id

    def process(job: Job) -> None:
        p = job.payload
        services.process(
            need_doc(job),
            max_tier=p.get("max_tier"),
            auto_post=bool(p.get("auto_post", False)),
        )

    def resume(job: Job) -> None:
        if services.runner is None:
            raise NonRetryable("worker has no pipeline runner")
        services.runner.resume(need_doc(job), dict(job.payload))

    def post(job: Job) -> None:
        services.post_document(need_doc(job), actor=parse_actor(job.payload.get("actor")))

    def followup(job: Job) -> None:
        fid = job.payload.get("followup_id")
        if not fid:
            raise NonRetryable("send_followup job without followup_id")
        send_followup(services.db, str(fid), webhook_url=followup_webhook_url, http=http)

    return {
        "process_document": process,
        "resume_document": resume,
        "post_bill": post,
        "send_followup": followup,
    }


# ---------------------------------------------------------------------------- worker
@dataclass
class Worker:
    queue: JobQueue
    handlers: Mapping[str, Handler]
    concurrency: int = 8
    poll_interval_s: float = 0.5
    base_backoff_s: float = 2.0
    max_backoff_s: float = 300.0
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")
    stale_after_s: int = 300
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _busy: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def kinds(self) -> list[str]:
        return sorted(self.handlers)

    def backoff(self, attempts: int) -> float:
        return min(self.max_backoff_s, self.base_backoff_s * 2 ** max(0, attempts - 1))

    def requeue_stale(self) -> int:
        requeue = getattr(self.queue, "requeue_stale", None)
        if requeue is None:
            return 0
        n = int(requeue(older_than_s=self.stale_after_s))
        if n:
            log.warning("requeued %d stale job(s) left running by a dead worker", n)
        return n

    def handle(self, job: Job) -> bool:
        """Run one job; True on success. Never raises (except BaseException kills)."""
        handler = self.handlers.get(job.kind)
        try:
            if handler is None:
                raise NonRetryable(f"no handler for job kind {job.kind!r}")
            handler(job)
        except Exception as exc:
            msg = mask_text(f"{type(exc).__name__}: {exc}") or type(exc).__name__
            if is_permanent(exc) or job.attempts >= job.max_attempts:
                log.error("job %s (%s) failed permanently: %s", job.id, job.kind, msg)
                self._fail_now(job, msg)
            else:
                delay = self.backoff(job.attempts)
                log.warning("job %s (%s) failed, retry in %.0fs: %s", job.id, job.kind, delay, msg)
                self.queue.fail(job.id, msg, retry_in_s=delay)
            return False
        self.queue.complete(job.id)
        return True

    def _fail_now(self, job: Job, msg: str) -> None:
        db = getattr(self.queue, "db", None)
        if isinstance(db, Database):
            with db.tx() as conn:
                conn.execute(
                    "update jobs set max_attempts = least(max_attempts, attempts) where id = %s",
                    (job.id,),
                )
        jobs = getattr(self.queue, "jobs", None)  # InMemoryJobQueue (tests, offline)
        if isinstance(jobs, dict) and job.id in jobs:
            jobs[job.id]["max_attempts"] = jobs[job.id]["attempts"]
        self.queue.fail(job.id, msg, retry_in_s=0)

    def run_once(self) -> bool:
        """Claim and run one job. Returns False when the queue had nothing ready."""
        job = self.queue.claim(self.worker_id, kinds=self.kinds)
        if job is None:
            return False
        with self._lock:
            self._busy += 1
        try:
            self.handle(job)
        finally:
            with self._lock:
                self._busy -= 1
        return True

    def run_until_idle(self, *, max_jobs: int = 10_000) -> int:
        """Single-threaded drain (tests, one-shot CLI). Returns jobs run."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:  # queue/database hiccup: back off, keep the thread alive
                log.exception("worker loop error")
                ran = False
            if not ran:
                self._stop.wait(self.poll_interval_s)

    def start(self) -> None:
        self.requeue_stale()
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        for t in self._threads:
            t.start()
        log.info(
            "worker %s started: %d threads, kinds=%s", self.worker_id, self.concurrency, self.kinds
        )

    def stop(self, timeout: float | None = 60.0) -> None:
        """Stop claiming; wait for in-flight jobs (graceful shutdown)."""
        self._stop.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        self._threads = []

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.0)


# ---------------------------------------------------------------------------- entry point
def _load_factory(path: str) -> Callable[[], LLMClient]:
    mod, _, attr = path.partition(":")
    obj: Any = importlib.import_module(mod)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def build_llm(settings: Settings, *, llm: LLMClient | None = None) -> LLMClient:
    from invoicedesk import wiring

    if llm is not None:
        return llm
    fallback: LLMClient | None = None
    if settings.mode == "offline":
        path = os.environ.get(LLM_FALLBACK_ENV)
        if path is None:
            try:
                fallback = _load_factory(DEFAULT_OFFLINE_FALLBACK)()
            except ImportError:
                log.warning("offline without %s: replay misses will fail", LLM_FALLBACK_ENV)
        elif path.strip().lower() not in ("", "none"):
            fallback = _load_factory(path)()
    return wiring.llm_client(settings, fallback=fallback)


def main(argv: list[str] | None = None, *, llm: LLMClient | None = None) -> int:
    from invoicedesk import wiring
    from invoicedesk.pipeline.deps import build_deps
    from invoicedesk.pipeline.events import PgNotifyPublisher
    from invoicedesk.pipeline.graph import PipelineRunner, postgres_checkpointer
    from invoicedesk.telemetry import setup_tracing, shutdown_tracing

    ap = argparse.ArgumentParser(
        prog="invoicedesk-worker", description="InvoiceDesk pipeline worker"
    )
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--once", action="store_true", help="drain the queue once and exit")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = get_settings()
    dsn = args.dsn or settings.database_url
    concurrency = args.concurrency or settings.worker_concurrency
    setup_tracing(settings)
    with ExitStack() as stack:
        db = stack.enter_context(Database(dsn, max_size=max(4, concurrency + 2)))
        saver = stack.enter_context(postgres_checkpointer(dsn, max_size=max(4, concurrency + 2)))
        publisher = PgNotifyPublisher(dsn)  # local Postgres, not an external service
        stack.callback(publisher.close)
        deps = build_deps(
            db,
            llm=build_llm(settings, llm=llm),
            settings=settings,
            publisher=publisher,
            accounting=wiring.accounting_adapter(settings),
        )
        runner = PipelineRunner(deps, saver)
        queue = PostgresJobQueue(db)
        services = PipelineServices(deps, runner=runner, queue=queue)
        http = stack.enter_context(httpx.Client(timeout=15.0))
        worker = Worker(
            queue,
            build_handlers(
                services, followup_webhook_url=os.environ.get(FOLLOWUP_WEBHOOK_ENV), http=http
            ),
            concurrency=concurrency,
        )
        if args.once:
            worker.requeue_stale()
            n = worker.run_until_idle()
            log.info("drained %d job(s)", n)
            shutdown_tracing()
            return 0

        def _graceful(signum: int, _frame: object) -> None:
            log.info("signal %d: finishing in-flight jobs", signum)
            worker._stop.set()

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
        worker.start()
        worker.wait()
        worker.stop()
    shutdown_tracing()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
