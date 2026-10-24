"""Document state machine (DESIGN §4.3) with evidence-gated transitions.

``transition()`` is the single function that decides whether a status change is legal. It
rejects anything not in :data:`TRANSITIONS` and, for the money-moving targets, demands
*evidence objects produced by deterministic code* (CLAUDE.md invariant 1):

* ``Validating -> ReadyToPay`` needs a :class:`RoutingDecision` built by :func:`route` from a
  complete set of rule results R1..R10 that all passed.
* ``Flagged -> ReadyToPay`` needs :class:`ApprovalEvidence`: two distinct human approvers,
  neither of them the uploader, a callback attestation (invariant 3), and the latest rule
  outcomes R1..R10 with no open ``review``-level failure: approvers release the *hold* (the
  ``flag`` failures they judged), never an unverified total, arithmetic, estimate, cap, date or
  vendor problem. Those are fixed first through ``Flagged -> Validating`` (ADR-0008).
* ``ReadyToPay -> Posted`` needs a :class:`PostingReceipt` with an idempotency key, and is
  refused for anonymous try-it uploads (they are never posted).

Actors of kind ``llm`` can never cause a transition; ``mcp`` actors can only post documents that
deterministic code already moved to ``ReadyToPay``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

__all__ = [
    "ALL_RULE_IDS",
    "KANBAN_COLUMNS",
    "TRANSITIONS",
    "Actor",
    "ApprovalEvidence",
    "ApprovalVote",
    "DocStatus",
    "DocumentFacts",
    "IllegalTransition",
    "PostingReceipt",
    "RoutingDecision",
    "RuleOutcome",
    "TransitionRecord",
    "kanban_column",
    "release_blockers",
    "route",
    "transition",
]


class DocStatus(StrEnum):
    RECEIVED = "Received"
    SCREENING = "Screening"
    QUARANTINED = "Quarantined"
    EXTRACTING = "Extracting"
    EXTRACTED = "Extracted"
    VALIDATING = "Validating"
    READY_TO_PAY = "ReadyToPay"
    NEEDS_REVIEW = "NeedsReview"
    FLAGGED = "Flagged"
    REJECTED = "Rejected"
    POSTED = "Posted"


S = DocStatus

#: Legal edges, exactly the §4.3 diagram.
TRANSITIONS: dict[DocStatus, frozenset[DocStatus]] = {
    S.RECEIVED: frozenset({S.SCREENING}),
    S.SCREENING: frozenset({S.QUARANTINED, S.EXTRACTING}),
    S.QUARANTINED: frozenset({S.FLAGGED}),
    S.EXTRACTING: frozenset({S.EXTRACTED}),
    S.EXTRACTED: frozenset({S.VALIDATING}),
    S.VALIDATING: frozenset({S.READY_TO_PAY, S.NEEDS_REVIEW, S.FLAGGED}),
    S.NEEDS_REVIEW: frozenset({S.VALIDATING, S.REJECTED}),
    S.FLAGGED: frozenset({S.READY_TO_PAY, S.VALIDATING, S.REJECTED}),
    S.READY_TO_PAY: frozenset({S.POSTED}),
    S.POSTED: frozenset(),
    S.REJECTED: frozenset(),
}

#: Kanban column per status (§4.3 note).
KANBAN_COLUMNS: dict[DocStatus, str] = {
    S.RECEIVED: "received",
    S.SCREENING: "received",
    S.EXTRACTING: "received",
    S.EXTRACTED: "extracted",
    S.VALIDATING: "validated",
    S.READY_TO_PAY: "ready_to_pay",
    S.NEEDS_REVIEW: "needs_review",
    S.FLAGGED: "flagged",
    S.QUARANTINED: "flagged",
    S.POSTED: "posted",
    S.REJECTED: "rejected",
}

ALL_RULE_IDS: tuple[str, ...] = tuple(f"R{i}" for i in range(1, 11))

Severity = Literal["pass", "review", "flag"]


def kanban_column(status: DocStatus | str) -> str:
    return KANBAN_COLUMNS[DocStatus(status)]


class IllegalTransition(Exception):
    """Raised for any transition that is not allowed or lacks the required evidence."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused a change. ``audit_label`` is what goes into ``audit_log.actor``."""

    kind: Literal["system", "user", "mcp", "llm"]
    id: str = "pipeline"

    @property
    def audit_label(self) -> str:
        return "system" if self.kind == "system" else f"{self.kind}:{self.id}"

    @staticmethod
    def system(component: str = "pipeline") -> Actor:
        return Actor("system", component)

    @staticmethod
    def user(user_id: str) -> Actor:
        return Actor("user", user_id)

    @staticmethod
    def mcp(client_id: str) -> Actor:
        return Actor("mcp", client_id)


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """Facts about the document that transition guards need."""

    document_id: str
    source: Literal["upload", "email", "seed", "mcp"]
    uploaded_by: str | None = None
    #: Non-null for anonymous try-it uploads; these are NEVER posted.
    session_id: str | None = None

    @property
    def is_visitor_upload(self) -> bool:
        return self.session_id is not None


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """Minimal view of a rule result that routing needs (the rules engine produces these)."""

    rule_id: str
    passed: bool
    severity: Severity
    message: str = ""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Output of :func:`route`. Only deterministic code builds it from rule outcomes."""

    target: DocStatus
    outcomes: tuple[RuleOutcome, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalVote:
    approver_id: str
    decision: Literal["approve", "reject"]
    callback_attested: bool
    kind: Literal["bank_change", "over_cap", "new_vendor", "duplicate", "injection", "other"]


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """Votes cast during the current hold plus the latest rule outcomes (R1..R10).

    Without ``outcomes`` the release is refused (fail closed): the guard must see that every
    remaining failure is a ``flag``-level hold the approvers are judging."""

    votes: tuple[ApprovalVote, ...]
    outcomes: tuple[RuleOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class PostingReceipt:
    system: Literal["qbo", "xero", "fake"]
    external_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """What the caller persists (status update + audit row) after a legal transition."""

    document_id: str
    from_status: DocStatus
    to_status: DocStatus
    actor: Actor
    reason: str
    details: dict[str, object] = field(default_factory=dict)


def route(outcomes: Iterable[RuleOutcome]) -> RoutingDecision:
    """Deterministic routing from rule outcomes (R1..R10 must all be present).

    Any ``flag`` failure -> Flagged; else any ``review`` failure -> NeedsReview; else ReadyToPay.
    Missing rules route to NeedsReview (fail closed).
    """
    outs = tuple(outcomes)
    present = {o.rule_id for o in outs}
    missing = [r for r in ALL_RULE_IDS if r not in present]
    failed = [o for o in outs if not o.passed]
    reasons = tuple(f"{o.rule_id}: {o.message}" for o in failed)
    if any(o.severity == "flag" for o in failed):
        return RoutingDecision(S.FLAGGED, outs, reasons)
    if failed or missing:
        extra = tuple(f"{r}: rule result missing" for r in missing)
        return RoutingDecision(S.NEEDS_REVIEW, outs, reasons + extra)
    return RoutingDecision(S.READY_TO_PAY, outs, ())


def _check_routing(ev: RoutingDecision, to: DocStatus) -> None:
    recomputed = route(ev.outcomes)
    if recomputed.target != to or ev.target != to:
        raise IllegalTransition(
            f"routing evidence says {recomputed.target}, refusing transition to {to}"
        )


def release_blockers(outcomes: Sequence[RuleOutcome]) -> tuple[str, ...]:
    """Why a two-person release of a Flagged document is still refused, as ``"R9: ..."``.

    Empty when the outcomes cover R1..R10 and every failure is ``flag``-level (the hold the
    approvers judge). A ``review``-level failure (R2 work order, R3 estimate, R5 arithmetic,
    R7 cap, R8 dates, R9 confidence, R1 unknown vendor) must be corrected or cleared first,
    exactly as it would be in NeedsReview; a missing rule result blocks too (fail closed).
    """
    present = {o.rule_id for o in outcomes}
    missing = tuple(f"{r}: rule result missing" for r in ALL_RULE_IDS if r not in present)
    open_review = tuple(
        f"{o.rule_id}: {o.message}" for o in outcomes if not o.passed and o.severity != "flag"
    )
    return open_review + missing


def _check_approvals(ev: ApprovalEvidence, facts: DocumentFacts) -> None:
    approvers = {v.approver_id for v in ev.votes if v.decision == "approve"}
    if any(v.decision == "reject" for v in ev.votes):
        raise IllegalTransition("an approver rejected; cannot release to ReadyToPay")
    if facts.uploaded_by is not None and facts.uploaded_by in approvers:
        raise IllegalTransition("the uploader cannot approve their own document")
    if len(approvers) < 2:
        raise IllegalTransition("Flagged -> ReadyToPay needs two distinct human approvers")
    if not any(v.callback_attested for v in ev.votes if v.decision == "approve"):
        raise IllegalTransition("callback-to-number-on-file attestation is required")
    blockers = release_blockers(ev.outcomes)
    if blockers:
        raise IllegalTransition(
            "release blocked until these are corrected or cleared: " + "; ".join(blockers)
        )


def transition(
    current: DocStatus | str,
    target: DocStatus | str,
    *,
    actor: Actor,
    facts: DocumentFacts,
    reason: str,
    evidence: RoutingDecision | ApprovalEvidence | PostingReceipt | None = None,
) -> TransitionRecord:
    """Validate a status change and return the record to persist; raise IllegalTransition."""
    cur, to = DocStatus(current), DocStatus(target)
    if to not in TRANSITIONS[cur]:
        raise IllegalTransition(f"{cur} -> {to} is not a legal transition")
    if actor.kind == "llm":
        raise IllegalTransition("LLM output can never change a document's status")

    details: dict[str, object] = {}
    if to is S.READY_TO_PAY:
        if cur is S.VALIDATING:
            if actor.kind != "system" or not isinstance(evidence, RoutingDecision):
                raise IllegalTransition("Validating -> ReadyToPay requires system RoutingDecision")
            _check_routing(evidence, to)
            details["rules"] = [o.rule_id for o in evidence.outcomes]
        else:  # Flagged -> ReadyToPay
            if actor.kind != "user" or not isinstance(evidence, ApprovalEvidence):
                raise IllegalTransition("Flagged -> ReadyToPay requires human ApprovalEvidence")
            _check_approvals(evidence, facts)
            details["approvers"] = sorted(
                {v.approver_id for v in evidence.votes if v.decision == "approve"}
            )
    elif to is S.POSTED:
        if not isinstance(evidence, PostingReceipt) or not evidence.idempotency_key:
            raise IllegalTransition("ReadyToPay -> Posted requires a PostingReceipt with a key")
        if facts.is_visitor_upload:
            raise IllegalTransition("visitor (try-it) uploads are never posted")
        details |= {"system": evidence.system, "external_id": evidence.external_id}
    elif to in (S.NEEDS_REVIEW, S.FLAGGED) and cur is S.VALIDATING:
        if not isinstance(evidence, RoutingDecision):
            raise IllegalTransition(f"Validating -> {to} requires a RoutingDecision")
        _check_routing(evidence, to)
        details["reasons"] = list(evidence.reasons)
    elif to is S.REJECTED and actor.kind not in ("user", "system"):
        raise IllegalTransition("only a human or the system can reject")
    elif cur in (S.NEEDS_REVIEW, S.FLAGGED) and to is S.VALIDATING and actor.kind != "user":
        raise IllegalTransition(f"{cur} -> Validating happens after a human correction")

    return TransitionRecord(facts.document_id, cur, to, actor, reason, details)
