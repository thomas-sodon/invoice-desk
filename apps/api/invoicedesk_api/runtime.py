"""Composition root for the API process (``Runtime``) and its embedded job runner.

Everything external sits behind a port with an offline implementation (CLAUDE.md, Data and
safety; docs/foundation.md decision 9):

=================  ===================================  ======================================
Port               offline (default, tests)              live
=================  ===================================  ======================================
LLM (pipeline)     ReplayLLM -> gold-backed OracleLLM    ``wiring.llm_client`` (Anthropic)
LLM (demo replay)  OracleLLM                             OracleLLM (recorded run, no cost)
LLM (wording)      refusing fake -> deterministic        ``wiring.llm_client``
                   template
Accounting         FakeAccounting                        QBO sandbox adapter
Storage            LocalStorage (HMAC-signed URLs)       Supabase Storage (``STORAGE_BACKEND``)
Turnstile          OfflineTurnstile (test secrets)       Cloudflare siteverify
file_url fetch     OfflineFetcher (refuses)              HttpsFetcher (SSRF-guarded)
n8n follow-ups     no-op without a webhook URL           POST to ``N8N_FOLLOWUP_WEBHOOK_URL``
Events             Postgres NOTIFY (local DB, not external) -> SSE
=================  ===================================  ======================================

Pipeline execution: uploads become ``api.process_document`` jobs on the Postgres queue that
this process's :class:`EmbeddedJobRunner` claims (``API_PIPELINE_MODE=embedded``), or
``process_document`` jobs for ``invoicedesk-worker`` (``worker``). Demo batches always run
embedded: they are evaluated with ``today = DEMO_TODAY`` and, offline, the oracle LLM.
Human actions resume LangGraph threads inline (``PipelineServices(runner=...)``), so the
response to a correction or approval already carries the re-routed status.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal

import httpx
from invoicedesk import wiring
from invoicedesk.config.settings import Settings, get_settings
from invoicedesk.contracts.accounting import AccountingAdapter
from invoicedesk.contracts.infra import EventPublisher, Job, Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import Preprocessor
from invoicedesk.db.connection import Database
from invoicedesk.db.queue import PostgresJobQueue
from invoicedesk.fakes.llm import FakeLLM, ReplayLLM
from invoicedesk.masking import mask_text
from invoicedesk.pipeline.deps import PipelineDeps, build_deps
from invoicedesk.pipeline.graph import PipelineRunner, postgres_checkpointer
from invoicedesk.pipeline.services import PipelineServices

from invoicedesk_api.fetch import HttpsFetcher, OfflineFetcher, UrlFetcher
from invoicedesk_api.settings import ApiSettings, get_api_settings
from invoicedesk_api.turnstile import TurnstileVerifier, build_turnstile

__all__ = [
    "EMBEDDED_KINDS",
    "EmbeddedJobRunner",
    "PipelineKind",
    "Runtime",
    "build_runtime",
    "demo_today",
    "load_oracle",
]

log = logging.getLogger("invoicedesk.api")

PipelineKind = Literal["default", "demo_live", "demo_replay"]
PROCESS_KIND = "api.process_document"
POST_KIND = "api.post_bill"
EMBEDDED_KINDS: tuple[str, ...] = (PROCESS_KIND, POST_KIND)


def demo_today() -> dt.date:
    """The demo batch's evaluation date (R8 ages invoices against it)."""
    try:
        from invoicedesk_data.demo import DEMO_TODAY
    except ImportError:  # pragma: no cover - the data package ships in the workspace
        return dt.datetime.now(dt.UTC).date()
    return DEMO_TODAY


def load_oracle(sets: tuple[str, ...] = ("demo", "synthetic-core", "adversarial")) -> LLMClient:
    """The data component's gold-backed oracle (offline demo / replay). Unknown documents get an
    empty extraction, so R9 holds them for a human."""
    from invoicedesk_data.oracle import OracleLLM

    return OracleLLM.from_gold(sets)


def _refusing_llm() -> LLMClient:
    """Offline wording client: always refuses, so wording falls back to the deterministic
    templates (no invented text offline)."""
    return FakeLLM(lambda _req: None)


@dataclass
class Runtime:
    """Everything a request handler needs. Built once per process (or per test)."""

    settings: Settings
    api: ApiSettings
    db: Database
    storage: Storage
    publisher: EventPublisher
    accounting: AccountingAdapter
    turnstile: TurnstileVerifier
    fetcher: UrlFetcher
    http: httpx.Client
    llm_factory: Callable[[], LLMClient]
    replay_llm_factory: Callable[[], LLMClient]
    wording_llm_factory: Callable[[], LLMClient]
    preprocessor: Preprocessor | None = None
    #: Inject a checkpointer (tests); otherwise PostgresSaver over the same database.
    checkpointer: Any = None
    clock: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))
    #: Test hook: end an SSE stream after this many events (None = stream forever).
    sse_max_events: int | None = None
    _stack: ExitStack = field(default_factory=ExitStack)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _services: dict[str, PipelineServices] = field(default_factory=dict)
    runner: EmbeddedJobRunner | None = None

    # ------------------------------------------------------------------ properties
    @property
    def tenant(self) -> str:
        return self.settings.tenant

    @property
    def offline(self) -> bool:
        return self.settings.mode == "offline"

    @property
    def pipeline_mode(self) -> Literal["embedded", "worker"]:
        if self.api.pipeline_mode is not None:
            return self.api.pipeline_mode
        return "embedded" if self.offline else "worker"

    @cached_property
    def queue(self) -> PostgresJobQueue:
        return PostgresJobQueue(self.db)

    @cached_property
    def wording_llm(self) -> LLMClient:
        return self.wording_llm_factory()

    @cached_property
    def default_llm(self) -> LLMClient:
        return self.llm_factory()

    @cached_property
    def replay_llm(self) -> LLMClient:
        return self.replay_llm_factory()

    def _checkpointer(self) -> Any:
        if self.checkpointer is None:
            self.checkpointer = self._stack.enter_context(
                postgres_checkpointer(self.db.dsn, max_size=self.api.embedded_concurrency + 4)
            )
        return self.checkpointer

    # ------------------------------------------------------------------ pipelines
    def _build(self, kind: PipelineKind) -> PipelineServices:
        llm = self.replay_llm if kind == "demo_replay" else self.default_llm
        deps: PipelineDeps = build_deps(
            self.db,
            llm=llm,
            storage=self.storage,
            settings=self.settings,
            publisher=self.publisher,
            accounting=self.accounting,
            preprocessor=self.preprocessor,
            today=demo_today if kind != "default" else None,
        )
        runner = PipelineRunner(deps, self._checkpointer())
        return PipelineServices(deps, runner=runner, queue=self.queue)

    def services(self, kind: PipelineKind = "default") -> PipelineServices:
        if kind == "demo_live" and self.offline:
            kind = "demo_replay"  # offline never calls a paid API
        with self._lock:
            svc = self._services.get(kind)
            if svc is None:
                svc = self._build(kind)
                self._services[kind] = svc
            return svc

    @staticmethod
    def _kind(row: dict[str, Any] | None) -> PipelineKind:
        if row is None or row["mode"] != "demo":
            return "default"
        return "demo_live" if row["llm"] == "live" else "demo_replay"

    def pipeline_kind_for(self, document_id: str) -> PipelineKind:
        with self.db.tx() as conn:
            row = conn.execute(
                "select b.mode, r.llm from documents d left join batches b on b.id = d.batch_id"
                " left join demo_runs r on r.batch_id = b.id where d.id = %s",
                (document_id,),
            ).fetchone()
        return self._kind(row)

    def pipeline_kind_for_batch(self, batch_id: str) -> PipelineKind:
        with self.db.tx() as conn:
            row = conn.execute(
                "select b.mode, r.llm from batches b left join demo_runs r on r.batch_id = b.id"
                " where b.id = %s",
                (batch_id,),
            ).fetchone()
        return self._kind(row)

    def services_for(self, document_id: str) -> PipelineServices:
        return self.services(self.pipeline_kind_for(document_id))

    # ------------------------------------------------------------------ dispatch
    def dispatch_process(
        self,
        document_id: str,
        *,
        demo: bool = False,
        max_tier: Literal["haiku", "sonnet", "opus"] | None = None,
    ) -> int:
        payload: dict[str, Any] = {"auto_post": False}
        if max_tier:
            payload["max_tier"] = max_tier
        if demo or self.pipeline_mode == "embedded":
            return self.queue.enqueue(
                PROCESS_KIND,
                document_id=document_id,
                payload=payload,
                dedupe_key=f"process:{document_id}",
            )
        return self.queue.enqueue(
            "process_document",
            document_id=document_id,
            payload=payload,
            dedupe_key=f"process:{document_id}",
        )

    def dispatch_post(self, document_id: str, *, actor_label: str) -> int:
        demo = self.pipeline_kind_for(document_id) != "default"
        kind = POST_KIND if demo or self.pipeline_mode == "embedded" else "post_bill"
        return self.queue.enqueue(
            kind,
            document_id=document_id,
            payload={"actor": actor_label},
            dedupe_key=f"post:{document_id}",
        )

    def run_pending_jobs(self, *, max_jobs: int = 1000) -> int:
        """Drain the embedded job kinds synchronously (tests, ``make demo`` scripts)."""
        runner = self.runner or EmbeddedJobRunner(self, concurrency=1)
        return runner.run_until_idle(max_jobs=max_jobs)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the embedded job threads (the app's lifespan decides whether to call this)."""
        if self.runner is None:
            self.runner = EmbeddedJobRunner(self, concurrency=self.api.embedded_concurrency)
            self.runner.start()

    def close(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self.runner = None
        close = getattr(self.publisher, "close", None)
        if callable(close):
            close()
        self._stack.close()
        self.http.close()


# ---------------------------------------------------------------------------- job runner
class EmbeddedJobRunner:
    """Claims the API's own job kinds (``api.*``) from the Postgres queue with SKIP LOCKED.

    Retries with exponential backoff; illegal transitions and rejected inputs fail at once (a
    retry cannot fix them). Error text is masked before it is stored (invariant 9)."""

    def __init__(self, rt: Runtime, *, concurrency: int = 4, poll_s: float = 0.5) -> None:
        self.rt = rt
        self.concurrency = concurrency
        self.poll_s = poll_s
        self.worker_id = f"api:{socket.gethostname()}:{id(self)}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def handle(self, job: Job) -> None:
        from invoicedesk.contracts.accounting import AccountingError
        from invoicedesk.contracts.preprocess import RejectedInput
        from invoicedesk.pipeline.posting import PostingError
        from invoicedesk.status import Actor, IllegalTransition

        q = self.rt.queue
        try:
            if not job.document_id:
                raise IllegalTransition(f"job {job.id} has no document")
            svc = self.rt.services_for(job.document_id)
            if job.kind == PROCESS_KIND:
                svc.process(
                    job.document_id,
                    max_tier=job.payload.get("max_tier"),
                    auto_post=False,
                )
            elif job.kind == POST_KIND:
                label = str(job.payload.get("actor") or "system")
                kind, _, ident = label.partition(":")
                actor = (
                    Actor.user(ident)
                    if kind == "user"
                    else Actor.mcp(ident)
                    if kind == "mcp"
                    else Actor.system("posting")
                )
                svc.post_document(job.document_id, actor=actor)
            else:
                raise IllegalTransition(f"unknown job kind {job.kind}")
        except Exception as exc:
            msg = mask_text(f"{type(exc).__name__}: {exc}") or type(exc).__name__
            permanent = isinstance(
                exc, (IllegalTransition, RejectedInput, PostingError, LookupError)
            ) or (isinstance(exc, AccountingError) and not exc.retryable)
            if permanent or job.attempts >= job.max_attempts:
                log.error("job %s (%s) failed: %s", job.id, job.kind, msg)
                with self.rt.db.tx() as conn:
                    conn.execute(
                        "update jobs set max_attempts = least(max_attempts, attempts)"
                        " where id = %s",
                        (job.id,),
                    )
                q.fail(job.id, msg, retry_in_s=0)
            else:
                q.fail(job.id, msg, retry_in_s=min(300.0, 2.0 * 2 ** max(0, job.attempts - 1)))
            return
        q.complete(job.id)

    def run_once(self) -> bool:
        job = self.rt.queue.claim(self.worker_id, kinds=list(EMBEDDED_KINDS))
        if job is None:
            return False
        self.handle(job)
        return True

    def run_until_idle(self, *, max_jobs: int = 1000) -> int:
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:
                log.exception("embedded job runner error")
                ran = False
            if not ran:
                self._stop.wait(self.poll_s)

    def start(self) -> None:
        self.rt.queue.requeue_stale(older_than_s=300)
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, name=f"api-jobs-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []


# ---------------------------------------------------------------------------- factory
def build_runtime(
    settings: Settings | None = None,
    api: ApiSettings | None = None,
    *,
    db: Database | None = None,
    storage: Storage | None = None,
    llm: LLMClient | None = None,
    replay_llm: LLMClient | None = None,
    wording_llm: LLMClient | None = None,
    accounting: AccountingAdapter | None = None,
    publisher: EventPublisher | None = None,
    turnstile: TurnstileVerifier | None = None,
    fetcher: UrlFetcher | None = None,
    http: httpx.Client | None = None,
    preprocessor: Preprocessor | None = None,
    checkpointer: Any = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> Runtime:
    from invoicedesk.pipeline.events import PgNotifyPublisher

    s = settings or get_settings()
    a = api or get_api_settings()
    database = db or Database(s.database_url, max_size=a.embedded_concurrency + 6)
    st = storage or wiring.storage(s)
    secret = s.turnstile_secret_key.get_secret_value() if s.turnstile_secret_key else None
    client = http or httpx.Client(timeout=15.0)

    def default_llm() -> LLMClient:
        if llm is not None:
            return llm
        if s.mode == "live":
            return wiring.llm_client(s)
        try:
            fallback = load_oracle()
        except ImportError:  # pragma: no cover - data package missing
            fallback = None
        return ReplayLLM(s.replay_dir, fallback=fallback)

    def replay() -> LLMClient:
        if replay_llm is not None:
            return replay_llm
        return load_oracle(("demo",))

    def wording() -> LLMClient:
        if wording_llm is not None:
            return wording_llm
        return wiring.llm_client(s) if s.mode == "live" else _refusing_llm()

    return Runtime(
        settings=s,
        api=a,
        db=database,
        storage=st,
        publisher=publisher or PgNotifyPublisher(database.dsn),
        accounting=accounting or wiring.accounting_adapter(s),
        turnstile=turnstile or build_turnstile(s.mode, secret, http=client),
        fetcher=fetcher or (HttpsFetcher() if s.mode == "live" else OfflineFetcher()),
        http=client,
        llm_factory=default_llm,
        replay_llm_factory=replay,
        wording_llm_factory=wording,
        preprocessor=preprocessor,
        checkpointer=checkpointer,
        clock=clock or (lambda: dt.datetime.now(dt.UTC)),
    )
