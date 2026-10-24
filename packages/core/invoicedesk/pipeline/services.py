"""Service layer for the API, the MCP facade and the worker (human actions + posting).

Every method records the human action through ``invoicedesk.db.repo`` (each write appends its
audit row), performs the resulting status change through ``repo.set_status`` (and therefore
``status.transition`` with its evidence gates), and only then resumes the document's LangGraph
thread. The resume payload is informational: the graph re-reads the status from the database.

Release rules (CLAUDE.md invariant 3; docs/foundation.md decisions 1-2):

* ``Flagged -> ReadyToPay`` only with two distinct *user* approvers (staff role ``approver`` or
  ``owner`` of the document's tenant), neither the uploader, with a callback attestation
  (``status._check_approvals``; bank-change approvals also need ``callback_attested`` on every
  row, enforced by the DB). No system or MCP actor can do it.
* Only votes cast *during the current hold* count (after the document entered Flagged, ordered
  by the serialized audit chain), so an approval recorded earlier in NeedsReview cannot be
  reused as the "second person". When R6 failed in the latest evaluation, only ``bank_change``
  approvals count: an unrelated ``other`` / ``over_cap`` vote never stands in for the
  bank-change attestation. Any reject vote on the document vetoes release (fail closed).
* ``NeedsReview`` over-cap / new-vendor holds clear by re-validation: an ``approve`` of kind
  ``over_cap`` or ``new_vendor`` moves the document back to Validating and R7 / R1 read it.
* A quarantined document was never extracted, so it can be rejected but not released: there is
  nothing validated to post (re-upload a clean copy instead).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from invoicedesk.contracts.accounting import AccountingAdapter
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.extractor import FieldResult
from invoicedesk.contracts.infra import JobQueue
from invoicedesk.contracts.tenant_data import ApprovalRecord
from invoicedesk.db import repo
from invoicedesk.db.connection import Conn
from invoicedesk.masking import mask_payload
from invoicedesk.pipeline.corrections import (
    Correction,
    apply_corrections_to_extraction,
    normalize_correction,
)
from invoicedesk.pipeline.deps import PipelineDeps
from invoicedesk.pipeline.events import emit_document
from invoicedesk.pipeline.graph import PipelineRunner, RunResult, load_corrections
from invoicedesk.pipeline.posting import PostResult, post_document
from invoicedesk.pipeline.steps import load_step_conn
from invoicedesk.status import (
    Actor,
    ApprovalEvidence,
    ApprovalVote,
    DocStatus,
    IllegalTransition,
    transition,
)

__all__ = [
    "APPROVAL_KINDS",
    "ApprovalResult",
    "CorrectionResult",
    "DuplicateVote",
    "NotAnApprover",
    "PipelineServices",
    "SelfApproval",
]

S = DocStatus
ApprovalKind = Literal["bank_change", "over_cap", "new_vendor", "duplicate", "injection", "other"]
APPROVAL_KINDS: frozenset[str] = frozenset(
    {"bank_change", "over_cap", "new_vendor", "duplicate", "injection", "other"}
)
#: NeedsReview holds a single non-uploader approval clears through re-validation (R7, R1).
REVALIDATING_KINDS: frozenset[str] = frozenset({"over_cap", "new_vendor"})


class SelfApproval(IllegalTransition):
    """The uploader tried to approve their own document (API: 403 ``self_approval``)."""


class NotAnApprover(IllegalTransition):
    """The voter is not a staff approver/owner of the document's tenant (API: 403)."""


class DuplicateVote(ValueError):
    """This approver already voted on this kind for this document."""


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    document_id: str
    status: str
    review_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    document_id: str
    status: str
    approvals: tuple[ApprovalRecord, ...]
    released: bool
    revalidated: bool


def _actor_of(approver: Actor | str) -> Actor:
    return approver if isinstance(approver, Actor) else Actor.user(approver)


class PipelineServices:
    """``runner`` resumes threads inline (API / tests); without it, resumes go to ``queue`` as
    ``resume_document`` jobs for the worker."""

    def __init__(
        self,
        deps: PipelineDeps,
        *,
        runner: PipelineRunner | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self.deps = deps
        self.db = deps.db
        self.runner = runner
        self.queue = queue

    # ------------------------------------------------------------------ ingest
    def enqueue_document(
        self,
        document_id: str,
        *,
        max_tier: Literal["haiku", "sonnet", "opus"] | None = None,
        auto_post: bool = False,
    ) -> int:
        if self.queue is None:
            raise RuntimeError("enqueue_document needs a job queue")
        payload: dict[str, Any] = {"auto_post": auto_post}
        if max_tier:
            payload["max_tier"] = max_tier
        return self.queue.enqueue(
            "process_document",
            document_id=document_id,
            payload=payload,
            dedupe_key=f"process:{document_id}",
        )

    def process(self, document_id: str, **kwargs: Any) -> RunResult:
        if self.runner is None:
            raise RuntimeError("process needs a PipelineRunner")
        return self.runner.process(document_id, **kwargs)

    def _continue(self, document_id: str, payload: dict[str, Any]) -> None:
        if self.runner is not None:
            self.runner.resume(document_id, payload)
        elif self.queue is not None:
            self.queue.enqueue("resume_document", document_id=document_id, payload=payload)
        elif self.deps.publisher is not None:
            emit_document(self.db, self.deps.publisher, document_id)

    def _status(self, document_id: str) -> str:
        with self.db.tx() as conn:
            return repo.get_document(conn, document_id).status.value

    # ------------------------------------------------------------------ corrections (M10)
    def apply_corrections(
        self,
        document_id: str,
        corrections: Sequence[Mapping[str, Any] | Correction],
        reviewer: str,
    ) -> CorrectionResult:
        """Record field corrections, store them as an eval example (split ``correction``),
        move NeedsReview -> Validating as the reviewer, and re-validate."""
        normalized = [
            c if isinstance(c, Correction) else normalize_correction(str(c["field"]), c["value"])
            for c in corrections
        ]
        if not normalized:
            raise ValueError("no corrections given")
        review_ids: list[str] = []
        with self.db.tx() as conn:
            doc = repo.get_document(conn, document_id, for_update=True)
            if doc.status is not S.NEEDS_REVIEW:
                raise IllegalTransition(
                    f"corrections apply to NeedsReview documents only (is {doc.status})"
                )
            current = {
                r["name"]: r
                for r in conn.execute(
                    "select name, value, word_ids, box, page from fields"
                    " where document_id = %s and final",
                    (document_id,),
                ).fetchall()
            }
            rows: list[FieldResult] = []
            for c in normalized:
                old = current.get(c.field)
                review_ids.append(
                    repo.record_review(
                        conn,
                        document_id,
                        field_name=c.field,
                        old_value=None if old is None else old["value"],
                        new_value=c.value,
                        reviewer_id=reviewer,
                    )
                )
                rows.append(
                    FieldResult(
                        name=c.field,
                        value=c.value,
                        raw_conf=1.0,
                        calibrated_conf=1.0,
                        word_ids=list(old["word_ids"] or []) if old else [],
                        box=tuple(old["box"]) if old and old["box"] else None,  # type: ignore[arg-type]
                        page=int(old["page"]) if old else 0,
                    )
                )
            # one final row per name: the last correction of a field wins
            by_name = {r.name: r for r in rows}
            repo.save_fields(conn, document_id, list(by_name.values()))
            self._write_correction_example(conn, doc, reviewer)
            repo.set_status(
                conn,
                document_id,
                S.VALIDATING,
                actor=Actor.user(reviewer),
                reason="Human corrected " + ", ".join(sorted(by_name)),
            )
        self._continue(document_id, {"action": "corrected", "fields": sorted(by_name)})
        return CorrectionResult(document_id, self._status(document_id), tuple(review_ids))

    def _write_correction_example(self, conn: Conn, doc: repo.DocumentRow, reviewer: str) -> None:
        """Trace-to-eval loop: the corrected extraction becomes a labeled example.

        Never for try-it visitor uploads (they are purged after 24h; an eval row would outlive
        them) or the demo seed batch (excluded from every eval split)."""
        if doc.session_id is not None or doc.source == "seed":
            return
        stored = load_step_conn(conn, doc.id, "extract")
        if stored is None:
            return
        base = InvoiceExtraction.model_validate(stored["extraction"])
        corrected, _ = apply_corrections_to_extraction(
            base, load_corrections(conn, doc.id), stored.get("calibrated", {})
        )
        vendor = conn.execute(
            "select value from fields where document_id = %s and name = 'vendor_id' and final",
            (doc.id,),
        ).fetchone()
        gold = {
            # Bank details masked to last 4 (invariant 9): gold for them is the master record.
            "extraction": _mask_remit(corrected.model_dump(mode="json")),
            "vendor_id": None if vendor is None else vendor["value"],
            "filename": doc.filename,
            "sha256": doc.sha256,
            "category": "human_correction",
            "reviewer_id": reviewer,
            "fields_corrected": sorted({c.field for c in load_corrections(conn, doc.id)}),
        }
        conn.execute(
            "insert into eval_examples (split, document_id, external_ref, gold_json, provenance)"
            " values ('correction', %s, %s, %s, 'human_correction')"
            " on conflict (split, external_ref) where external_ref is not null"
            " do update set gold_json = excluded.gold_json",
            (doc.id, f"correction/{doc.id}", json.dumps(gold)),
        )

    # ------------------------------------------------------------------ approvals (US-5)
    def record_approval(
        self,
        document_id: str,
        *,
        kind: str,
        approver: Actor | str,
        decision: Literal["approve", "reject"],
        callback_attested: bool,
        note: str | None = None,
    ) -> ApprovalResult:
        actor = _actor_of(approver)
        if actor.kind != "user":
            raise IllegalTransition("only a human approver can vote on a held document")
        if kind not in APPROVAL_KINDS:
            raise ValueError(f"unknown approval kind {kind!r}")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        if kind == "bank_change" and decision == "approve" and not callback_attested:
            raise IllegalTransition(
                "a bank-change approval needs the callback-to-number-on-file attestation"
            )
        released = revalidate = False
        with self.db.tx() as conn:
            doc = repo.get_document(conn, document_id, for_update=True)
            if doc.status not in (S.FLAGGED, S.NEEDS_REVIEW):
                raise IllegalTransition(f"nothing to approve: document is {doc.status}")
            if doc.uploaded_by is not None and doc.uploaded_by == actor.id:
                raise SelfApproval("the uploader cannot approve their own document")
            _require_approver_role(conn, doc, actor)
            try:
                with conn.transaction():  # savepoint: keep the outer tx usable on a conflict
                    repo.record_approval(
                        conn,
                        document_id,
                        kind=kind,
                        approver_id=actor.id,
                        decision=decision,
                        callback_attested=callback_attested,
                        note=note,
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateVote("this approver already voted on this hold") from exc
            except psycopg.errors.CheckViolation as exc:  # DB trigger (self-approval/callback)
                raise SelfApproval(str(exc).splitlines()[0]) from exc
            approvals = tuple(repo.list_approvals(conn, document_id))

            if doc.status is S.FLAGGED and decision == "approve":
                extracted = load_step_conn(conn, document_id, "extract") is not None
                evidence = ApprovalEvidence(votes=_release_votes(conn, document_id, approvals))
                if extracted and _two_person_rule_met(doc, actor, evidence):
                    repo.set_status(
                        conn,
                        document_id,
                        S.READY_TO_PAY,
                        actor=actor,
                        reason="Two-person approval with callback attestation",
                        evidence=evidence,
                    )
                    released = True
            elif (
                doc.status is S.NEEDS_REVIEW
                and decision == "approve"
                and kind in REVALIDATING_KINDS
            ):
                repo.set_status(
                    conn,
                    document_id,
                    S.VALIDATING,
                    actor=actor,
                    reason=f"{kind.replace('_', '-')} approved; re-validating",
                )
                revalidate = True
        if released or revalidate:
            self._continue(document_id, {"action": "approved", "kind": kind})
        elif self.deps.publisher is not None:
            emit_document(self.db, self.deps.publisher, document_id, types=("document.updated",))
        return ApprovalResult(
            document_id, self._status(document_id), approvals, released, revalidate
        )

    # ------------------------------------------------------------------ reject
    def reject(self, document_id: str, *, actor: Actor | str, reason: str) -> str:
        a = _actor_of(actor)
        with self.db.tx() as conn:
            repo.set_status(conn, document_id, S.REJECTED, actor=a, reason=reason or "Rejected")
        self._continue(document_id, {"action": "rejected"})
        return self._status(document_id)

    # ------------------------------------------------------------------ posting (M11)
    def post_document(
        self,
        document_id: str,
        adapter: AccountingAdapter | None = None,
        *,
        actor: Actor | None = None,
    ) -> PostResult:
        adp = adapter or self.deps.accounting
        if adp is None:
            raise RuntimeError("post_document needs an AccountingAdapter")
        res = post_document(
            self.db,
            document_id,
            adp,
            self.deps.storage,
            actor=actor,
            expense_account_ref=self.deps.expense_account_ref,
        )
        if self.deps.publisher is not None:
            emit_document(
                self.db, self.deps.publisher, document_id, message=f"Posted to {res.system}"
            )
        return res

    def ready_documents(self, batch_id: str) -> list[str]:
        with self.db.tx() as conn:
            rows = conn.execute(
                "select id from documents where batch_id = %s and status = 'ReadyToPay'"
                " and session_id is null order by created_at, id",
                (batch_id,),
            ).fetchall()
        return [str(r["id"]) for r in rows]

    def enqueue_post(self, document_id: str, *, actor: Actor | None = None) -> int:
        if self.queue is None:
            raise RuntimeError("enqueue_post needs a job queue")
        return self.queue.enqueue(
            "post_bill",
            document_id=document_id,
            payload={"actor": (actor or Actor.system("posting")).audit_label},
            dedupe_key=f"post:{document_id}",
        )

    def post_ready(
        self,
        batch_id: str,
        adapter: AccountingAdapter | None = None,
        *,
        actor: Actor | None = None,
    ) -> list[int] | list[PostResult]:
        """Queue (or, without a queue, run) a post for every ReadyToPay document in a batch."""
        ids = self.ready_documents(batch_id)
        if self.queue is not None:
            return [self.enqueue_post(d, actor=actor) for d in ids]
        return [self.post_document(d, adapter, actor=actor) for d in ids]

    # ------------------------------------------------------------------ comparison (M13)
    def comparison(
        self, batch_id: str, *, truth: Mapping[str, Iterable[str]] | None = None
    ) -> dict[str, Any]:
        from invoicedesk.pipeline.naive import compare_batch

        return compare_batch(self.db, batch_id, truth=truth)


def _require_approver_role(conn: Conn, doc: repo.DocumentRow, actor: Actor) -> None:
    """Clerks upload, correct and reject; only approvers and owners vote (api-contract roles)."""
    row = conn.execute(
        "select role::text as role from staff_users where id::text = %s and tenant_id = %s",
        (actor.id, doc.tenant_id),
    ).fetchone()
    if row is None or row["role"] not in ("approver", "owner"):
        raise NotAnApprover("only an approver or owner of this tenant can vote on a hold")


def _hold_started_audit_id(conn: Conn, document_id: str) -> int:
    row = conn.execute(
        "select coalesce(max(id), 0) as id from audit_log where document_id = %s"
        " and action = 'status.changed' and after_json->>'status' = 'Flagged'",
        (document_id,),
    ).fetchone()
    return 0 if row is None else int(row["id"])


def _r6_failed(conn: Conn, document_id: str) -> bool:
    row = conn.execute(
        "select 1 from rule_results where document_id = %s and rule_id = 'R6' and not passed"
        " and evaluation = (select max(evaluation) from rule_results where document_id = %s)",
        (document_id, document_id),
    ).fetchone()
    return row is not None


def _release_votes(
    conn: Conn, document_id: str, approvals: Sequence[ApprovalRecord]
) -> tuple[ApprovalVote, ...]:
    """The votes that may release a Flagged document (see module docstring).

    ``approvals`` has one row per (kind, approver); its audit row (``approval.<decision>``,
    actor ``user:<id>``, ``after.kind``) tells when it was cast relative to the Flagged
    transition, because audit appends are serialized and their ids are commit-ordered.
    """
    start = _hold_started_audit_id(conn, document_id)
    in_hold = {
        (r["actor"].partition(":")[2], r["kind"])
        for r in conn.execute(
            "select actor, after_json->>'kind' as kind from audit_log where document_id = %s"
            " and action in ('approval.approve', 'approval.reject') and id > %s",
            (document_id, start),
        ).fetchall()
    }
    bank_only = _r6_failed(conn, document_id)
    votes: list[ApprovalVote] = []
    for a in approvals:
        vote = ApprovalVote(a.approver_id, a.decision, a.callback_attested, a.kind)
        if a.decision == "reject":
            votes.append(vote)  # any reject vetoes, whenever it was cast (fail closed)
        elif (a.approver_id, a.kind) in in_hold and (not bank_only or a.kind == "bank_change"):
            votes.append(vote)
    return tuple(votes)


def _two_person_rule_met(doc: repo.DocumentRow, actor: Actor, evidence: ApprovalEvidence) -> bool:
    """Dry-run of the transition guard (pure): True when the release would be accepted."""
    try:
        transition(
            S.FLAGGED,
            S.READY_TO_PAY,
            actor=actor,
            facts=repo.document_facts(doc),
            reason="check",
            evidence=evidence,
        )
    except IllegalTransition:
        return False
    return True


def _mask_remit(extraction: dict[str, Any]) -> dict[str, Any]:
    out = dict(extraction)
    remit = dict(out["remit_to_bank_details"])
    remit["value"] = mask_payload({"remit_to_bank_details": remit["value"]})[
        "remit_to_bank_details"
    ]
    out["remit_to_bank_details"] = remit
    return out
