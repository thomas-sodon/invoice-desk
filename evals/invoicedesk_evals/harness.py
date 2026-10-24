"""Run one matrix config over examples with the real pipeline, in-process (no job queue).

For ``pipeline: invoicedesk`` configs every document goes through the production LangGraph
graph (preprocess -> screen -> extract_and_score -> validate) built by
:func:`invoicedesk.pipeline.build_deps` around the injected LLM client: the real preprocessor,
guardrail screeners, tiered extractor, calibrator and rules engine. Only the checkpointer is
in-memory (checkpoints do not change results) and nothing is posted: ``ReadyToPay`` is the
auto-post decision being measured.

Config knobs are applied *around* the pipeline, never inside it:

* single-tier configs (A, B, C, E) run with ``max_tier="haiku"`` and :class:`ConfigLLM` remaps
  the tier-1 request's role to the chosen model's role, so exactly one extraction call is made;
* ``caching: false`` (D') strips the system blocks' cache breakpoints;
* ``images: false`` (E, F) strips image blocks (and skips the uploads);
* ``extractor: azure_di`` (G) swaps in
  :class:`~invoicedesk_evals.azure_invoice.AzureInvoiceExtractor`.

``pipeline: naive`` (N) calls :func:`invoicedesk.pipeline.naive.build_naive_request` on the
same preprocessing and "posts" whatever comes back (dry run).

Each document runs in its own workspace database cloned from a migrated, seeded template, with
its declared priors processed first. Preprocessing is deterministic, so its output is cached by
file hash across repeats and configs (its original wall time is added back to latency).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from invoicedesk.config.models import TIER_ROLE, ModelRole
from invoicedesk.config.tenant import RulesConfig
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.extractor import Extractor
from invoicedesk.contracts.infra import Storage, StoredObject
from invoicedesk.contracts.llm import (
    FileImageBlock,
    InlineImageBlock,
    LLMCallError,
    LLMClient,
    LLMRequest,
    LLMResponse,
    SystemBlock,
)
from invoicedesk.contracts.preprocess import PreprocessedDocument, Preprocessor
from invoicedesk.db import repo
from invoicedesk.db.connection import Database
from invoicedesk.db.migrate import apply_migrations
from invoicedesk.status import DocStatus
from invoicedesk.storage import InMemoryStorage
from invoicedesk_data.gold import GoldRecord
from pydantic import BaseModel

from invoicedesk_evals.datasets import Example
from invoicedesk_evals.matrix import ConfigSpec
from invoicedesk_evals.model import DocRun, LLMCall
from invoicedesk_evals.pg import PgHost

__all__ = [
    "TENANT_ID",
    "CachingPreprocessor",
    "ConfigLLM",
    "Harness",
    "LockedStorage",
    "collect_run",
]

log = logging.getLogger(__name__)

TENANT_ID = "maple_row"
_READY = DocStatus.READY_TO_PAY.value


# ----------------------------------------------------------------------------- LLM wrapper
class ConfigLLM:
    """Applies a config's knobs to every request and meters every call per document."""

    def __init__(self, inner: LLMClient, spec: ConfigSpec, *, simulated_latency: bool) -> None:
        self.inner = inner
        self.spec = spec
        self.simulated_latency = simulated_latency
        self._lock = threading.Lock()
        self._calls: dict[str, list[LLMCall]] = {}
        self._remap: ModelRole | None = None
        if spec.extractor == "claude" and len(spec.tiers) == 1 and spec.tiers[0] != "haiku":
            self._remap = TIER_ROLE[spec.tiers[0]]

    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        if not self.spec.images:
            # Images are stripped from every request, so never pay for the upload.
            return "file_unused_" + hashlib.sha256(data).hexdigest()[:24]
        return self.inner.upload_file(data, filename=filename, media_type=media_type)

    def _transform(self, req: LLMRequest) -> LLMRequest:
        update: dict[str, Any] = {}
        if self._remap is not None and req.role == ModelRole.EXTRACT_T1:
            update["role"] = self._remap
        if not self.spec.caching:
            update["system"] = tuple(SystemBlock(text=b.text, cache=False) for b in req.system)
        if not self.spec.images:
            update["user"] = tuple(
                b for b in req.user if not isinstance(b, FileImageBlock | InlineImageBlock)
            )
        return req.model_copy(update=update) if update else req

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        req = self._transform(request)
        resp = self.inner.parse(req, output_model)
        self._record(req, resp)
        return resp

    def _record(self, req: LLMRequest, resp: LLMResponse) -> None:
        doc = req.metadata.get("document_id", "")
        call = LLMCall(
            role=str(req.role.value),
            model_id=resp.model_id,
            cost_usd=str(resp.cost_usd),
            latency_ms=int(resp.latency_ms),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_input_tokens=resp.usage.cache_read_input_tokens,
            refused=resp.refused,
        )
        with self._lock:
            self._calls.setdefault(doc, []).append(call)

    def calls_for(self, document_id: str) -> list[LLMCall]:
        with self._lock:
            return list(self._calls.pop(document_id, []))


# ----------------------------------------------------------------------------- storage
class LockedStorage(InMemoryStorage):
    """``InMemoryStorage`` safe for the harness's worker threads (``list`` iterates the dict
    while other threads put and delete)."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        with self._lock:
            return super().put(key, data, content_type=content_type)

    def delete(self, key: str) -> None:
        with self._lock:
            super().delete(key)

    def list(self, prefix: str) -> list[str]:
        with self._lock:
            return super().list(prefix)


# ----------------------------------------------------------------------------- preprocess
class CachingPreprocessor:
    """Memoizes a deterministic preprocessor by file hash. On a hit the cached output is
    re-keyed to the new document id and its page renders are copied to the new keys."""

    def __init__(self, inner: Preprocessor, storage: Storage) -> None:
        self.inner = inner
        self.storage = storage
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[PreprocessedDocument, dict[str, bytes], int]] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        #: document_id -> original preprocessing wall time when served from the cache.
        self.replayed_ms: dict[str, int] = {}
        self.hits = 0
        self.misses = 0

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def preprocess(
        self, data: bytes, *, mime: str, document_id: str, filename: str | None = None
    ) -> PreprocessedDocument:
        key = hashlib.sha256(data).hexdigest() + ":" + mime
        with self._key_lock(key):
            hit = self._cache.get(key)
            if hit is None:
                t0 = time.perf_counter()
                doc = self.inner.preprocess(
                    data, mime=mime, document_id=document_id, filename=filename
                )
                ms = int((time.perf_counter() - t0) * 1000)
                pages = {p.storage_key: self.storage.get(p.storage_key) for p in doc.pages}
                self._cache[key] = (doc, pages, ms)
                self.misses += 1
                return doc
        cached, pages, ms = hit
        self.hits += 1
        old = cached.document_id
        new_pages = []
        for p in cached.pages:
            new_key = p.storage_key.replace(old, document_id)
            self.storage.put(new_key, pages[p.storage_key], content_type="image/png")
            new_pages.append(p.model_copy(update={"storage_key": new_key}))
        with self._lock:
            self.replayed_ms[document_id] = ms
        return cached.model_copy(update={"document_id": document_id, "pages": new_pages})


# ----------------------------------------------------------------------------- harness
class Harness:
    def __init__(
        self,
        host: PgHost,
        spec: ConfigSpec,
        llm: LLMClient,
        *,
        rules_config: RulesConfig,
        today: dt.date,
        simulated_latency: bool,
        workers: int = 1,
        preprocessor: Preprocessor | None = None,
        storage: Storage | None = None,
        extractor: Extractor | None = None,
        keep_workspaces: bool = False,
    ) -> None:
        self.host = host
        self.spec = spec
        self.llm = ConfigLLM(llm, spec, simulated_latency=simulated_latency)
        self.rules_config = rules_config
        self.today = today
        self.workers = max(1, workers)
        if storage is None and isinstance(preprocessor, CachingPreprocessor):
            storage = preprocessor.storage  # cached renders are copied into this storage
        self.storage = storage or LockedStorage()
        if preprocessor is None:
            from invoicedesk.preprocess import build_preprocessor

            preprocessor = build_preprocessor(self.storage, key_prefix=TENANT_ID)
        self.preprocessor = (
            preprocessor
            if isinstance(preprocessor, CachingPreprocessor)
            else CachingPreprocessor(preprocessor, self.storage)
        )
        #: Shared replacement reader (config G); ``None`` = the production TieredExtractor.
        self.extractor = extractor
        self.keep_workspaces = keep_workspaces
        self._template: str | None = None

    # -- template -----------------------------------------------------------------------
    def setup(self) -> None:
        if self._template is not None or self.spec.pipeline == "naive":
            return
        from invoicedesk_data.seed import seed_database

        name = self.host.unique_name("evaltpl")
        dsn = self.host.create_db(name)
        apply_migrations(dsn)
        db = Database(dsn, min_size=1, max_size=2)
        try:
            seed_database(db, TENANT_ID)
        finally:
            db.close()
        self._template = name

    def close(self) -> None:
        if self._template is not None and not self.keep_workspaces:
            self.host.drop_db(self._template)
        self._template = None

    def __enter__(self) -> Harness:
        self.setup()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- running ----------------------------------------------------------------------
    def run(
        self,
        examples: Sequence[Example],
        *,
        repeat: int,
        on_done: Callable[[DocRun], None] | None = None,
    ) -> list[DocRun]:
        self.setup()

        def one(ex: Example) -> DocRun:
            try:
                run = (
                    self._run_naive(ex, repeat)
                    if self.spec.pipeline == "naive"
                    else self._run_pipeline(ex, repeat)
                )
            except LLMCallError as exc:
                run = self._error_run(ex, repeat, f"llm:{exc.kind}")
            except Exception as exc:
                log.exception("eval of %s failed", ex.id)
                run = self._error_run(ex, repeat, f"{type(exc).__name__}: {str(exc)[:200]}")
            if on_done is not None:
                on_done(run)
            return run

        if self.workers == 1:
            return [one(ex) for ex in examples]
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="eval") as pool:
            return list(pool.map(one, examples))

    def _error_run(self, ex: Example, repeat: int, error: str) -> DocRun:
        return DocRun(
            example_id=ex.id,
            config=self.spec.name,
            repeat=repeat,
            status="Error",
            auto_posted=False,
            error=error,
        )

    def _finish_costs(self, run: DocRun, doc_id: str, wall_ms: int) -> DocRun:
        calls = self.llm.calls_for(doc_id)
        cost = sum((Decimal(c.cost_usd) for c in calls), Decimal(0))
        extra = getattr(self.extractor, "cost_for", None)
        if callable(extra):  # non-LLM readers (Azure DI) bill per page
            cost += Decimal(str(extra(doc_id)))
        llm_ms = sum(c.latency_ms for c in calls)
        replayed = self.preprocessor.replayed_ms.pop(doc_id, 0)
        wall = wall_ms + replayed
        latency = wall + llm_ms if self.llm.simulated_latency else wall
        return run.model_copy(
            update={
                "calls": calls,
                "cost_usd": str(cost),
                "wall_ms": wall,
                "llm_ms": llm_ms,
                "latency_ms": latency,
            }
        )

    # -- naive (config N) -------------------------------------------------------------
    def _run_naive(self, ex: Example, repeat: int) -> DocRun:
        from invoicedesk.extract.files_api import PageImageUploader
        from invoicedesk.pipeline.naive import build_naive_request

        data = ex.file().read_bytes()
        doc_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        pre = self.preprocessor.preprocess(
            data, mime=ex.gold.mime, document_id=doc_id, filename=ex.gold.filename
        )
        images = PageImageUploader(self.llm, self.storage).image_blocks(pre)
        email = ex.gold.email.body_text if ex.gold.email is not None else None
        posted = False
        extraction: dict[str, Any] | None = None
        try:
            resp = self.llm.parse(build_naive_request(pre, images, email), InvoiceExtraction)
            if resp.parsed is not None and not resp.refused:
                extraction = InvoiceExtraction.model_validate(resp.parsed).model_dump(mode="json")
                posted = True  # the baseline auto-posts everything it can read (dry run)
        except LLMCallError as exc:
            if exc.kind != "validation":
                raise
        wall = int((time.perf_counter() - t0) * 1000)
        self._cleanup_storage(doc_id)
        run = DocRun(
            example_id=ex.id,
            config=self.spec.name,
            repeat=repeat,
            status="Posted" if posted else "NotPosted",
            auto_posted=posted,
            extraction=extraction,
            tiers=["haiku"],
            escalated_to="haiku",
            has_boxes=False,
        )
        return self._finish_costs(run, doc_id, wall)

    # -- InvoiceDesk pipeline -----------------------------------------------------------
    def _deps(self, db: Database) -> Any:
        from invoicedesk.pipeline.deps import build_deps

        deps = build_deps(
            db,
            llm=self.llm,
            storage=self.storage,
            preprocessor=self.preprocessor,
            extractor=self.extractor,
            rules_config=self.rules_config,
            today=lambda: self.today,
        )
        deps.rules_configs[TENANT_ID] = self.rules_config
        deps.max_tier = self.spec.max_tier
        return deps

    def _insert(self, db: Database, rec: GoldRecord, data: bytes, tag: str) -> str:
        key = f"{TENANT_ID}/uploads/{tag}/{rec.filename}"
        self.storage.put(key, data, content_type=rec.mime)
        with db.tx() as conn:
            doc_id = repo.insert_document(
                conn,
                tenant_id=TENANT_ID,
                source="email" if rec.email is not None else "upload",
                sha256=hashlib.sha256(data).hexdigest(),
                storage_path=key,
                mime=rec.mime,
                filename=rec.filename,
            )
            if rec.email is not None:
                row = conn.execute(
                    "insert into email_threads (tenant_id, message_id, from_addr, subject,"
                    " body_text) values (%s,%s,%s,%s,%s) on conflict (tenant_id, message_id)"
                    " do update set body_text = excluded.body_text returning id",
                    (
                        TENANT_ID,
                        rec.email.message_id,
                        rec.email.from_addr,
                        rec.email.subject,
                        rec.email.body_text,
                    ),
                ).fetchone()
                assert row is not None
                conn.execute(
                    "insert into document_threads (document_id, thread_id) values (%s,%s)",
                    (doc_id, row["id"]),
                )
        return doc_id

    def _run_pipeline(self, ex: Example, repeat: int) -> DocRun:
        from invoicedesk.pipeline.graph import PipelineRunner
        from langgraph.checkpoint.memory import InMemorySaver

        assert self._template is not None
        name = self.host.unique_name("evalws")
        dsn = self.host.create_db(name, template=self._template)
        db = Database(dsn, min_size=1, max_size=3)
        tag = uuid.uuid4().hex[:12]
        try:
            runner = PipelineRunner(self._deps(db), InMemorySaver())
            for prior in ex.support:
                pid = self._insert(db, prior, ex_file(prior).read_bytes(), f"{tag}-{prior.id}")
                runner.process(pid, max_tier=self.spec.max_tier)
                self.llm.calls_for(pid)  # support documents are not billed to the example
                self.preprocessor.replayed_ms.pop(pid, None)
                self._cleanup_storage(pid)
            data = ex.file().read_bytes()
            doc_id = self._insert(db, ex.gold, data, f"{tag}-{ex.gold.id}")
            t0 = time.perf_counter()
            runner.process(doc_id, max_tier=self.spec.max_tier)
            wall = int((time.perf_counter() - t0) * 1000)
            run = collect_run(db, doc_id, example_id=ex.id, config=self.spec.name, repeat=repeat)
            self._cleanup_storage(doc_id)
            return self._finish_costs(run, doc_id, wall)
        finally:
            for key in self.storage.list(f"{TENANT_ID}/uploads/{tag}"):
                self.storage.delete(key)
            db.close()
            if not self.keep_workspaces:
                self.host.drop_db(name)

    def _cleanup_storage(self, doc_id: str) -> None:
        for key in self.storage.list(f"{TENANT_ID}/renders/{doc_id}"):
            self.storage.delete(key)


def ex_file(rec: GoldRecord) -> Any:
    from invoicedesk_data.gold import file_path

    return file_path(rec)


# ----------------------------------------------------------------------------- collection
def _step(conn: Any, doc_id: str, step: str) -> dict[str, Any] | None:
    row = conn.execute(
        "select output_json from pipeline_steps where document_id = %s and step = %s"
        " order by round desc limit 1",
        (doc_id, step),
    ).fetchone()
    return None if row is None else dict(row["output_json"])


def collect_run(db: Database, doc_id: str, *, example_id: str, config: str, repeat: int) -> DocRun:
    """Read what the pipeline decided for one document from its workspace database."""
    with db.tx() as conn:
        doc = conn.execute("select status from documents where id = %s", (doc_id,)).fetchone()
        assert doc is not None
        status = str(doc["status"])
        screen = _step(conn, doc_id, "screen") or {}
        extract = _step(conn, doc_id, "extract")
        fields = conn.execute(
            "select name, value, box, page from fields where document_id = %s and final",
            (doc_id,),
        ).fetchall()
        ev = conn.execute(
            "select max(evaluation) as e from rule_results where document_id = %s", (doc_id,)
        ).fetchone()
        rules = (
            conn.execute(
                "select rule_id, passed, severity, message from rule_results"
                " where document_id = %s and evaluation = %s",
                (doc_id, ev["e"]),
            ).fetchall()
            if ev is not None and ev["e"] is not None
            else []
        )
        screen_rows = conn.execute(
            "select detector, verdict, categories from screen_results where document_id = %s",
            (doc_id,),
        ).fetchall()
        tiers = conn.execute(
            "select tier from extractions where document_id = %s order by created_at, tier",
            (doc_id,),
        ).fetchall()
    boxes: dict[str, dict[str, Any]] = {}
    vendor_resolved = False
    vendor_id: str | None = None
    for f in fields:
        if f["name"] == "vendor_id":
            vendor_resolved = True
            vendor_id = f["value"]
        elif f["box"] is not None:
            boxes[f["name"]] = {"box": list(f["box"]), "page": int(f["page"] or 0)}
    failed = {r["rule_id"]: r["severity"] for r in rules if not r["passed"]}
    return DocRun(
        example_id=example_id,
        config=config,
        repeat=repeat,
        status=status,
        auto_posted=status == _READY,
        quarantined=bool(screen.get("quarantined")),
        # A failed extraction is kept (every field empty) so it counts against recall.
        extraction=None if extract is None else extract["extraction"],
        calibrated={k: float(v) for k, v in ((extract or {}).get("calibrated") or {}).items()},
        vendor_resolved=vendor_resolved,
        vendor_id=vendor_id,
        boxes=boxes,
        failed_rules=failed,
        rule_messages={r["rule_id"]: r["message"] for r in rules if not r["passed"]},
        screen=[
            {
                "detector": s["detector"],
                "verdict": s["verdict"],
                "categories": list(s["categories"] or []),
            }
            for s in screen_rows
        ],
        tiers=[str(t["tier"]) for t in tiers],
        escalated_to=(extract or {}).get("escalated_to"),
    )
