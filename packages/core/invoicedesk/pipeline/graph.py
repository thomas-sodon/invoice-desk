"""The document pipeline as a LangGraph ``StateGraph`` (DESIGN §4.1-4.3, §5.5; M9, M10, M20).

::

    preprocess -> screen --quarantined--> review_interrupt
                     \\--> extract_and_score -> validate --ReadyToPay--> post -> END
                                                   \\--NeedsReview/Flagged--> review_interrupt
    review_interrupt --(human acted; status re-read from the DB)--> validate | post | END

Invariants this module holds:

* **The LLM reads; code decides (1).** Routing is ``status.route`` over all ten rule results;
  every status change goes through ``repo.set_status`` -> ``status.transition`` with evidence.
* **Screening before extraction (8).** A quarantined document goes Quarantined -> Flagged and the
  graph never reaches ``extract_and_score`` (the node also refuses defensively).
* **Idempotent nodes.** Each node writes its effects, its transition and a ``pipeline_steps``
  marker in one transaction under the document row lock; a node re-run after a crash returns
  the stored output. The ``PostgresSaver`` checkpointer (``thread_id = document_id``) resumes
  the graph from the last completed node.
* **Human review is ``interrupt()``.** The services layer records the human action (correction,
  approval, rejection) and the resulting status change, then resumes the thread; the review node
  re-reads the status from the database, so the resume payload can never move a document.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Literal, Required, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.extractor import FieldResult
from invoicedesk.contracts.preprocess import PreprocessedDocument
from invoicedesk.contracts.rules import DocumentInfo, RuleContext
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.db import repo
from invoicedesk.db.connection import Conn
from invoicedesk.masking import mask_payload
from invoicedesk.pipeline.corrections import Correction, apply_corrections_to_extraction
from invoicedesk.pipeline.deps import PipelineDeps
from invoicedesk.pipeline.events import emit_document
from invoicedesk.pipeline.steps import load_step, load_step_conn, save_step
from invoicedesk.rules.engine import resolved_vendor_id, route_results
from invoicedesk.status import Actor, DocStatus
from invoicedesk.telemetry import document_span

__all__ = [
    "PipelineRunner",
    "PipelineState",
    "RunResult",
    "build_graph",
    "email_body_for",
    "empty_extraction",
    "load_corrections",
    "postgres_checkpointer",
]

log = logging.getLogger(__name__)

S = DocStatus
SYSTEM = Actor.system("pipeline")


class PipelineState(TypedDict, total=False):
    document_id: Required[str]
    tenant_id: str
    max_tier: Literal["haiku", "sonnet", "opus"]
    auto_post: bool
    #: PreprocessedDocument JSON (words, text layer, page keys; images live in Storage).
    doc: dict[str, Any]
    screening: dict[str, Any]
    quarantined: bool
    #: Final (unmasked) InvoiceExtraction JSON from the model; corrections overlay at validate.
    extraction: dict[str, Any] | None
    calibrated: dict[str, float]
    #: Validation round (0 first; +1 for every human action that re-validates).
    round: int
    #: Status after the last node that routed.
    route: str
    review: Any


# ---------------------------------------------------------------------------- helpers
def email_body_for(conn: Conn, document_id: str) -> str | None:
    row = conn.execute(
        "select t.body_text from document_threads dt join email_threads t on t.id = dt.thread_id"
        " where dt.document_id = %s order by t.received_at limit 1",
        (document_id,),
    ).fetchone()
    body = None if row is None else row["body_text"]
    return body if body and body.strip() else None


def load_corrections(conn: Conn, document_id: str) -> list[Correction]:
    """Every human correction for the document, oldest first (later ones win)."""
    rows = conn.execute(
        "select field_name, new_value from reviews where document_id = %s order by created_at, id",
        (document_id,),
    ).fetchall()
    return [Correction(r["field_name"], r["new_value"]) for r in rows]


def empty_extraction(note: str) -> InvoiceExtraction:
    """What validation sees when no tier produced a reading: every field null at confidence 0,
    so R9 holds the document for a human instead of failing the job forever."""

    def ref() -> dict[str, Any]:
        return {"word_ids": [], "fallback_box": None}

    def tf() -> dict[str, Any]:
        return {"value": None, "confidence": 0.0, "source": ref()}

    def mf() -> dict[str, Any]:
        return {"value_cents": None, "currency": "USD", "confidence": 0.0, "source": ref()}

    def df() -> dict[str, Any]:
        return {"value_iso": None, "confidence": 0.0, "source": ref()}

    return InvoiceExtraction.model_validate(
        {
            "doc_type": "other",
            "vendor_name": tf(),
            "vendor_address": tf(),
            "vendor_tax_id_last4": tf(),
            "invoice_number": tf(),
            "invoice_date": df(),
            "due_date": df(),
            "work_order_number": tf(),
            "property_or_unit": tf(),
            "line_items": [],
            "subtotal": mf(),
            "tax": mf(),
            "total": mf(),
            "remit_to_bank_details": tf(),
            "handwriting_present": False,
            "legibility_issues": [note],
            "embedded_instructions": [],
        }
    )


def _masked_field(f: FieldResult) -> FieldResult:
    """``fields`` rows feed the UI: bank details are stored masked to the last 4 (inv. 9)."""
    if f.name != "remit_to_bank_details" or f.value is None:
        return f
    masked = mask_payload({"remit_to_bank_details": f.value})["remit_to_bank_details"]
    return f.model_copy(update={"value": masked})


def _add_cost(conn: Conn, doc: repo.DocumentRow, usd: Decimal) -> None:
    """Running per-document cost (+ the public spend ledger for try-it uploads)."""
    if usd <= 0:
        return
    conn.execute(
        "update documents set total_cost_usd = total_cost_usd + %s where id = %s", (usd, doc.id)
    )


def _visitor_spend(conn: Conn, doc: repo.DocumentRow, usd: Decimal) -> None:
    if usd <= 0 or doc.session_id is None:
        return
    repo.add_spend(conn, scope="global", key="tryit", usd=usd)
    repo.add_spend(conn, scope="session", key=doc.session_id, usd=usd)


def _last_status_audit(conn: Conn, document_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "select id, row_hash from audit_log where document_id = %s and action = 'status.changed'"
        " order by id desc limit 1",
        (document_id,),
    ).fetchone()
    return None if row is None else {"id": int(row["id"]), "row_hash": row["row_hash"]}


def _need(state: PipelineState, key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"pipeline state is missing {key!r}")
    return value


def _status(deps: PipelineDeps, document_id: str) -> DocStatus:
    with deps.db.tx() as conn:
        return repo.get_document(conn, document_id).status


# ---------------------------------------------------------------------------- nodes
class _Nodes:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    def _emit(self, document_id: str, message: str | None = None) -> None:
        if self.deps.publisher is not None:
            emit_document(self.deps.db, self.deps.publisher, document_id, message=message)

    # -- preprocess -----------------------------------------------------------------
    def preprocess(self, state: PipelineState) -> dict[str, Any]:
        deps, doc_id = self.deps, state["document_id"]
        with document_span("pipeline.preprocess", **{"invoicedesk.document_id": doc_id}) as span:
            stored = load_step(deps.db, doc_id, "preprocess")
            if stored is not None:
                span.set(**{"invoicedesk.replayed": True})
                return {"doc": stored["doc"], "tenant_id": stored["tenant_id"]}
            with deps.db.tx() as conn:
                row = repo.get_document(conn, doc_id)
            data = deps.storage.get(row.storage_path)
            prefix = f"visitor/{row.session_id}" if row.session_id else row.tenant_id
            pre = _run_preprocessor(deps, data, row, prefix)
            out = {"doc": pre.model_dump(mode="json"), "tenant_id": row.tenant_id}
            with deps.db.tx() as conn:
                doc = repo.get_document(conn, doc_id, for_update=True)
                again = load_step_conn(conn, doc_id, "preprocess")
                if again is not None:
                    return {"doc": again["doc"], "tenant_id": again["tenant_id"]}
                conn.execute(
                    "update documents set phash = coalesce(phash, %s), pages = %s,"
                    " updated_at = now() where id = %s",
                    (pre.phash, max(1, min(5, len(pre.pages))), doc_id),
                )
                if doc.status is S.RECEIVED:
                    repo.set_status(
                        conn, doc_id, S.SCREENING, actor=SYSTEM, reason="Preprocessed; screening"
                    )
                save_step(conn, doc_id, "preprocess", out)
            span.set(**{"invoicedesk.pages": len(pre.pages), "invoicedesk.words": len(pre.words)})
        self._emit(doc_id)
        return out

    # -- screen ---------------------------------------------------------------------
    def screen(self, state: PipelineState) -> dict[str, Any]:
        from invoicedesk.guardrails import run_screening

        deps, doc_id = self.deps, state["document_id"]
        with document_span("pipeline.screen", **{"invoicedesk.document_id": doc_id}) as span:
            stored = load_step(deps.db, doc_id, "screen")
            if stored is not None:
                span.set(**{"invoicedesk.replayed": True})
                return _screen_update(stored)
            doc = PreprocessedDocument.model_validate(_need(state, "doc"))
            with deps.db.tx() as conn:
                email = email_body_for(conn, doc_id)
            outcome = run_screening(doc, deps.screeners, email_body=email)
            for r in outcome.results:
                with document_span(
                    f"screen.{r.detector}",
                    **{
                        "invoicedesk.document_id": doc_id,
                        "invoicedesk.detector": r.detector,
                        "invoicedesk.verdict": r.verdict,
                        "invoicedesk.categories": ",".join(r.categories),
                        "invoicedesk.spans": len(r.spans),
                        "invoicedesk.cost_usd": r.cost_usd,
                    },
                ):
                    pass
            cost = sum((Decimal(r.cost_usd) for r in outcome.results), Decimal(0))
            message: str | None = None
            with deps.db.tx() as conn:
                row = repo.get_document(conn, doc_id, for_update=True)
                again = load_step_conn(conn, doc_id, "screen")
                if again is not None:
                    return _screen_update(again)
                repo.record_screen_results(conn, doc_id, row.tenant_id, outcome.results)
                _add_cost(conn, row, cost)
                _visitor_spend(conn, row, cost)
                block: dict[str, Any] | None = None
                if outcome.quarantined:
                    detectors = sorted(
                        {r.detector for r in outcome.results if r.verdict != "clean"}
                    )
                    message = "Quarantined by screening: " + ", ".join(detectors)
                    repo.set_status(conn, doc_id, S.QUARANTINED, actor=SYSTEM, reason=message)
                    block = _last_status_audit(conn, doc_id)
                    repo.set_status(
                        conn,
                        doc_id,
                        S.FLAGGED,
                        actor=SYSTEM,
                        reason="Quarantined documents are held for two-person review",
                    )
                else:
                    repo.set_status(
                        conn, doc_id, S.EXTRACTING, actor=SYSTEM, reason="Screening clean"
                    )
                out = {
                    "screening": outcome.model_dump(mode="json"),
                    "quarantined": outcome.quarantined,
                    "block_audit": block,
                }
                save_step(conn, doc_id, "screen", out)
            span.set(
                **{
                    "invoicedesk.quarantined": outcome.quarantined,
                    "invoicedesk.cost_usd": cost,
                }
            )
            if block is not None:
                # detector -> block -> audit row, visible as one chain in the trace (M20).
                with document_span(
                    "pipeline.quarantine",
                    **{
                        "invoicedesk.document_id": doc_id,
                        "invoicedesk.blocked": True,
                        "invoicedesk.audit_row_id": block["id"],
                        "invoicedesk.audit_row_hash": block["row_hash"],
                    },
                ):
                    pass
        self._emit(doc_id, message)
        return _screen_update(out)

    # -- extract_and_score ----------------------------------------------------------
    def extract_and_score(self, state: PipelineState) -> dict[str, Any]:
        from invoicedesk.extract import ExtractionFailed

        deps, doc_id = self.deps, state["document_id"]
        screening = ScreeningOutcome.model_validate(_need(state, "screening"))
        if screening.quarantined or state.get("quarantined"):
            # Unreachable through the graph's edges; refuse anyway (invariant 8).
            raise RuntimeError("quarantined documents never reach extraction")
        with document_span(
            "pipeline.extract_and_score", **{"invoicedesk.document_id": doc_id}
        ) as span:
            stored = load_step(deps.db, doc_id, "extract")
            if stored is not None:
                span.set(**{"invoicedesk.replayed": True})
                return _extract_update(stored)
            doc = PreprocessedDocument.model_validate(_need(state, "doc"))
            with deps.db.tx() as conn:
                email = email_body_for(conn, doc_id)
            failed: str | None = None
            try:
                outcome = deps.extractor.extract(
                    doc,
                    screening=screening,
                    email_body=email,
                    max_tier=state.get("max_tier", deps.max_tier),
                )
                tiers, final, fields = outcome.tiers, outcome.final, outcome.fields
                escalated = outcome.escalated_to
            except ExtractionFailed as exc:
                failed = "No model produced a valid reading; every field needs a human."
                tiers, final, fields = exc.tiers, empty_extraction(failed), []
                escalated = tiers[-1].tier if tiers else "haiku"
            calibrated = {f.name: f.calibrated_conf for f in fields}
            cost = sum((Decimal(t.cost_usd) for t in tiers), Decimal(0))
            out = {
                "extraction": final.model_dump(mode="json"),
                "calibrated": calibrated,
                "escalated_to": escalated,
                "failed": failed,
            }
            with deps.db.tx() as conn:
                row = repo.get_document(conn, doc_id, for_update=True)
                again = load_step_conn(conn, doc_id, "extract")
                if again is not None:
                    return _extract_update(again)
                for t in tiers:
                    repo.record_extraction(conn, doc_id, t)
                _visitor_spend(conn, row, cost)
                if fields:
                    repo.save_fields(conn, doc_id, [_masked_field(f) for f in fields])
                if row.status is S.EXTRACTING:
                    repo.set_status(
                        conn,
                        doc_id,
                        S.EXTRACTED,
                        actor=SYSTEM,
                        reason=failed or f"Extracted (escalated to {escalated})",
                    )
                save_step(conn, doc_id, "extract", out)
            span.set(
                **{
                    "invoicedesk.escalated_to": escalated,
                    "invoicedesk.tiers": len(tiers),
                    "invoicedesk.cost_usd": cost,
                    "invoicedesk.extraction_failed": failed is not None,
                }
            )
        self._emit(doc_id, failed)
        return _extract_update(out)

    # -- validate -------------------------------------------------------------------
    def validate(self, state: PipelineState) -> dict[str, Any]:
        from invoicedesk.status import IllegalTransition

        deps, doc_id = self.deps, state["document_id"]
        rnd = int(state.get("round", 0))
        with document_span(
            "pipeline.validate", **{"invoicedesk.document_id": doc_id, "invoicedesk.round": rnd}
        ) as span:
            stored = load_step(deps.db, doc_id, "validate", rnd)
            if stored is not None:
                span.set(**{"invoicedesk.replayed": True})
                return {"route": stored["route"]}
            base = InvoiceExtraction.model_validate(_need(state, "extraction"))
            screening = ScreeningOutcome.model_validate(_need(state, "screening"))
            message: str | None = None
            with deps.db.tx() as conn:
                doc = repo.get_document(conn, doc_id, for_update=True)
                again = load_step_conn(conn, doc_id, "validate", rnd)
                if again is not None:
                    return {"route": again["route"]}
                if doc.status is S.EXTRACTED:
                    repo.set_status(conn, doc_id, S.VALIDATING, actor=SYSTEM, reason="Validating")
                    doc = repo.get_document(conn, doc_id)
                if doc.status is not S.VALIDATING:
                    # A human already moved it (e.g. rejected); nothing to evaluate.
                    return {"route": doc.status.value}
                # Serialize validation per tenant: R4 keys duplicates on the resolved vendor_id
                # that validate itself writes, so two copies of one invoice validated at the
                # same moment would otherwise not see each other and both pass.
                conn.execute(
                    "select pg_advisory_xact_lock(hashtext(%s))",
                    (f"invoicedesk.validate:{doc.tenant_id}",),
                )
                extraction, conf = apply_corrections_to_extraction(
                    base, load_corrections(conn, doc_id), state.get("calibrated", {})
                )
                ctx = RuleContext(
                    extraction=extraction,
                    calibrated_conf=conf,
                    screening=screening,
                    tenant=repo.load_tenant_data(conn, doc.tenant_id, exclude_document_id=doc_id),
                    config=deps.rules_config(doc.tenant_id),
                    document=DocumentInfo(
                        document_id=doc_id,
                        sha256=doc.sha256,
                        phash=doc.phash,
                        uploaded_by=doc.uploaded_by,
                        source=doc.source,
                    ),
                    today=deps.today(),
                    approvals=tuple(repo.list_approvals(conn, doc_id)),
                    email_body_present=email_body_for(conn, doc_id) is not None,
                )
                results = deps.rules_engine.evaluate(ctx)
                evaluation = repo.record_rule_results(conn, doc_id, doc.tenant_id, results)
                vendor_id = resolved_vendor_id(results)
                repo.save_fields(
                    conn,
                    doc_id,
                    [
                        FieldResult(
                            name="vendor_id",
                            value=vendor_id,
                            raw_conf=1.0,
                            calibrated_conf=1.0 if vendor_id else 0.0,
                        )
                    ],
                )
                decision = route_results(results)
                failed = [r for r in results if not r.passed]
                message = failed[0].message if failed else "All checks passed"
                try:
                    repo.set_status(
                        conn,
                        doc_id,
                        decision.target,
                        actor=SYSTEM,
                        reason=message,
                        evidence=decision,
                    )
                except IllegalTransition:  # pragma: no cover - route() and transition() agree
                    log.exception("routing evidence refused for %s", doc_id)
                    raise
                out = {"route": decision.target.value, "evaluation": evaluation}
                save_step(conn, doc_id, "validate", out, rnd)
            span.set(
                **{
                    "invoicedesk.route": decision.target.value,
                    "invoicedesk.failed_rules": ",".join(r.rule_id for r in failed),
                }
            )
        self._emit(doc_id, message)
        return {"route": out["route"]}

    # -- review_interrupt -----------------------------------------------------------
    def review_interrupt(self, state: PipelineState) -> dict[str, Any]:
        doc_id = state["document_id"]
        status = _status(self.deps, doc_id)
        payload: Any = None
        if status in (S.NEEDS_REVIEW, S.FLAGGED):
            # Pauses the thread; resumed by services after a human action. The payload is
            # informational only: the next step is decided from the status in the DB.
            payload = interrupt(
                {"document_id": doc_id, "status": status.value, "round": state.get("round", 0)}
            )
            status = _status(self.deps, doc_id)
        update: dict[str, Any] = {"route": status.value, "review": payload}
        if status is S.VALIDATING:
            update["round"] = int(state.get("round", 0)) + 1
        return update

    # -- post -----------------------------------------------------------------------
    def post(self, state: PipelineState) -> dict[str, Any]:
        from invoicedesk.pipeline.posting import post_document

        deps, doc_id = self.deps, state["document_id"]
        status = _status(deps, doc_id)
        if status is not S.READY_TO_PAY or not state.get("auto_post") or deps.accounting is None:
            return {"route": status.value}
        with document_span("pipeline.post", **{"invoicedesk.document_id": doc_id}) as span:
            res = post_document(
                deps.db,
                doc_id,
                deps.accounting,
                deps.storage,
                actor=Actor.system("posting"),
                expense_account_ref=deps.expense_account_ref,
            )
            span.set(**{"invoicedesk.bill_id": res.bill_id, "invoicedesk.system": res.system})
        self._emit(doc_id, f"Posted to {res.system}")
        return {"route": S.POSTED.value}


def _run_preprocessor(
    deps: PipelineDeps, data: bytes, row: repo.DocumentRow, prefix: str
) -> PreprocessedDocument:
    from invoicedesk.preprocess import DefaultPreprocessor

    p = deps.preprocessor
    if isinstance(p, DefaultPreprocessor):
        return p.preprocess(
            data, mime=row.mime, document_id=row.id, filename=row.filename, storage_prefix=prefix
        )
    return p.preprocess(data, mime=row.mime, document_id=row.id, filename=row.filename)


def _screen_update(out: dict[str, Any]) -> dict[str, Any]:
    return {"screening": out["screening"], "quarantined": bool(out["quarantined"])}


def _extract_update(out: dict[str, Any]) -> dict[str, Any]:
    return {"extraction": out["extraction"], "calibrated": out["calibrated"]}


# ---------------------------------------------------------------------------- edges
def _after_screen(state: PipelineState) -> str:
    return "review_interrupt" if state.get("quarantined") else "extract_and_score"


def _after_validate(state: PipelineState) -> str:
    route = state.get("route")
    if route == S.READY_TO_PAY:
        return "post"
    if route in (S.NEEDS_REVIEW, S.FLAGGED):
        return "review_interrupt"
    return END


def _after_review(state: PipelineState) -> str:
    route = state.get("route")
    if route == S.VALIDATING:
        return "validate"
    if route == S.READY_TO_PAY:
        return "post"
    if route in (S.NEEDS_REVIEW, S.FLAGGED):
        return "review_interrupt"
    return END


def build_graph(deps: PipelineDeps, checkpointer: BaseCheckpointSaver | None) -> Any:
    nodes = _Nodes(deps)
    g: StateGraph = StateGraph(PipelineState)
    g.add_node("preprocess", nodes.preprocess)
    g.add_node("screen", nodes.screen)
    g.add_node("extract_and_score", nodes.extract_and_score)
    g.add_node("validate", nodes.validate)
    g.add_node("review_interrupt", nodes.review_interrupt)
    g.add_node("post", nodes.post)
    g.add_edge(START, "preprocess")
    g.add_edge("preprocess", "screen")
    g.add_conditional_edges("screen", _after_screen, ["review_interrupt", "extract_and_score"])
    g.add_edge("extract_and_score", "validate")
    g.add_conditional_edges("validate", _after_validate, ["post", "review_interrupt", END])
    g.add_conditional_edges(
        "review_interrupt", _after_review, ["validate", "post", "review_interrupt", END]
    )
    g.add_edge("post", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------- checkpointer
@contextmanager
def postgres_checkpointer(dsn: str, *, max_size: int = 10) -> Iterator[BaseCheckpointSaver]:
    """``PostgresSaver`` over its own autocommit pool; ``setup()`` creates its tables."""
    from langgraph.checkpoint.postgres import PostgresSaver

    pool: ConnectionPool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    try:
        saver = PostgresSaver(pool)  # pyright: ignore[reportArgumentType]
        saver.setup()
        yield saver
    finally:
        pool.close()


# ---------------------------------------------------------------------------- runner
class RunResult(TypedDict):
    document_id: str
    status: str
    waiting_for_human: bool


class PipelineRunner:
    """Runs and resumes document threads (``thread_id = document_id``)."""

    def __init__(self, deps: PipelineDeps, checkpointer: BaseCheckpointSaver) -> None:
        self.deps = deps
        self.checkpointer = checkpointer
        self.graph = build_graph(deps, checkpointer)

    @staticmethod
    def config(document_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": document_id}}

    def _result(self, document_id: str) -> RunResult:
        snap = self.graph.get_state(self.config(document_id))
        return {
            "document_id": document_id,
            "status": _status(self.deps, document_id).value,
            "waiting_for_human": bool(snap.interrupts),
        }

    def _invoke(self, document_id: str, value: Any) -> None:
        self.graph.invoke(value, self.config(document_id), durability="sync")

    def process(
        self,
        document_id: str,
        *,
        max_tier: Literal["haiku", "sonnet", "opus"] | None = None,
        auto_post: bool = False,
    ) -> RunResult:
        """Start the thread, or continue it after a crash. A thread already waiting for a
        human (interrupt) or finished is left alone."""
        snap = self.graph.get_state(self.config(document_id))
        if not snap.values:
            state: PipelineState = {
                "document_id": document_id,
                "max_tier": max_tier or self.deps.max_tier,
                "auto_post": auto_post,
                "round": 0,
            }
            self._invoke(document_id, state)
        elif snap.next and not snap.interrupts:
            self._invoke(document_id, None)  # resume from the last checkpoint
        return self._result(document_id)

    def resume(self, document_id: str, payload: dict[str, Any] | None = None) -> RunResult:
        """Continue after a human action already recorded (and status changed) in the DB."""
        cfg = self.config(document_id)
        snap = self.graph.get_state(cfg)
        if not snap.values:
            raise LookupError(f"document {document_id} has no pipeline thread")
        if snap.interrupts:
            self._invoke(document_id, Command(resume=payload or {}))
        elif snap.next:
            self._invoke(document_id, None)
        else:
            # Thread finished earlier (e.g. ended at ReadyToPay or after a reject): route from
            # the review node again, which re-reads the status from the database.
            status = _status(self.deps, document_id)
            update: dict[str, Any] = {"review": payload, "route": status.value}
            if status is S.VALIDATING:
                update["round"] = int(snap.values.get("round", 0)) + 1
            self.graph.update_state(cfg, update, as_node="review_interrupt")
            snap = self.graph.get_state(cfg)
            if snap.next:
                self._invoke(document_id, None)
        return self._result(document_id)

    def state(self, document_id: str) -> PipelineState:
        return self.graph.get_state(self.config(document_id)).values

    def delete_thread(self, document_id: str) -> None:
        """Drop checkpoints (visitor purge after 24h)."""
        self.checkpointer.delete_thread(document_id)
