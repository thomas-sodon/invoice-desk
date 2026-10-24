import pytest
from invoicedesk.status import (
    ALL_RULE_IDS,
    TRANSITIONS,
    Actor,
    ApprovalEvidence,
    ApprovalVote,
    DocStatus,
    DocumentFacts,
    IllegalTransition,
    PostingReceipt,
    RoutingDecision,
    RuleOutcome,
    kanban_column,
    release_blockers,
    route,
    transition,
)

S = DocStatus
SYS = Actor.system()
FACTS = DocumentFacts("d1", "upload", uploaded_by="u-uploader")
VISITOR = DocumentFacts("d2", "upload", session_id="sess-1")


def all_pass() -> list[RuleOutcome]:
    return [RuleOutcome(r, True, "pass") for r in ALL_RULE_IDS]


def with_fail(rule: str, sev: str) -> list[RuleOutcome]:
    return [RuleOutcome(r, r != rule, "pass" if r != rule else sev, "x") for r in ALL_RULE_IDS]  # type: ignore[arg-type]


def test_diagram_edges_exact() -> None:
    edges = {(a, b) for a, bs in TRANSITIONS.items() for b in bs}
    assert len(edges) == 15
    assert (S.FLAGGED, S.VALIDATING) in edges  # ADR-0008: fix review items while held
    assert (S.QUARANTINED, S.EXTRACTING) not in edges
    assert (S.NEEDS_REVIEW, S.READY_TO_PAY) not in edges
    assert (S.EXTRACTED, S.READY_TO_PAY) not in edges


def test_route() -> None:
    assert route(all_pass()).target is S.READY_TO_PAY
    assert route(with_fail("R7", "review")).target is S.NEEDS_REVIEW
    assert route(with_fail("R6", "flag")).target is S.FLAGGED
    assert route(all_pass()[:-1]).target is S.NEEDS_REVIEW  # missing R10 fails closed
    d = route(with_fail("R3", "review"))
    assert d.reasons == ("R3: x",)


def test_simple_path() -> None:
    for a, b in [
        (S.RECEIVED, S.SCREENING),
        (S.SCREENING, S.EXTRACTING),
        (S.SCREENING, S.QUARANTINED),
        (S.QUARANTINED, S.FLAGGED),
        (S.EXTRACTING, S.EXTRACTED),
        (S.EXTRACTED, S.VALIDATING),
    ]:
        rec = transition(a, b, actor=SYS, facts=FACTS, reason="t")
        assert rec.to_status is b


def test_illegal_edge() -> None:
    with pytest.raises(IllegalTransition):
        transition(S.RECEIVED, S.POSTED, actor=SYS, facts=FACTS, reason="t")
    with pytest.raises(IllegalTransition):
        transition(S.QUARANTINED, S.EXTRACTING, actor=SYS, facts=FACTS, reason="t")


def test_llm_actor_never_moves() -> None:
    with pytest.raises(IllegalTransition):
        transition(S.RECEIVED, S.SCREENING, actor=Actor("llm", "haiku"), facts=FACTS, reason="t")


def test_ready_to_pay_requires_routing_evidence() -> None:
    with pytest.raises(IllegalTransition):
        transition(S.VALIDATING, S.READY_TO_PAY, actor=SYS, facts=FACTS, reason="t")
    with pytest.raises(IllegalTransition):  # user cannot shortcut
        transition(
            S.VALIDATING,
            S.READY_TO_PAY,
            actor=Actor.user("u"),
            facts=FACTS,
            reason="t",
            evidence=route(all_pass()),
        )
    bad = route(with_fail("R6", "flag"))
    forged = RoutingDecision(S.READY_TO_PAY, bad.outcomes)
    with pytest.raises(IllegalTransition):
        transition(
            S.VALIDATING, S.READY_TO_PAY, actor=SYS, facts=FACTS, reason="t", evidence=forged
        )
    rec = transition(
        S.VALIDATING, S.READY_TO_PAY, actor=SYS, facts=FACTS, reason="t", evidence=route(all_pass())
    )
    assert rec.details["rules"] == list(ALL_RULE_IDS)


def test_validating_to_review_and_flag_need_matching_routing() -> None:
    rec = transition(
        S.VALIDATING,
        S.NEEDS_REVIEW,
        actor=SYS,
        facts=FACTS,
        reason="t",
        evidence=route(with_fail("R9", "review")),
    )
    assert rec.details["reasons"] == ["R9: x"]
    with pytest.raises(IllegalTransition):
        transition(
            S.VALIDATING,
            S.FLAGGED,
            actor=SYS,
            facts=FACTS,
            reason="t",
            evidence=route(with_fail("R9", "review")),
        )
    with pytest.raises(IllegalTransition):
        transition(S.VALIDATING, S.FLAGGED, actor=SYS, facts=FACTS, reason="t")


def _vote(uid: str, *, cb: bool = True, decision: str = "approve") -> ApprovalVote:
    return ApprovalVote(uid, decision, cb, "bank_change")  # type: ignore[arg-type]


R6_HOLD = tuple(with_fail("R6", "flag"))


def test_flagged_release_two_person_rule() -> None:
    ok = ApprovalEvidence((_vote("a1"), _vote("a2", cb=False)), R6_HOLD)
    rec = transition(
        S.FLAGGED, S.READY_TO_PAY, actor=Actor.user("a2"), facts=FACTS, reason="t", evidence=ok
    )
    assert rec.details["approvers"] == ["a1", "a2"]
    cases = [
        ApprovalEvidence((_vote("a1"),), R6_HOLD),  # one approver
        ApprovalEvidence((_vote("a1"), _vote("a1")), R6_HOLD),  # same person twice
        ApprovalEvidence((_vote("a1"), _vote("u-uploader")), R6_HOLD),  # uploader
        ApprovalEvidence((_vote("a1", cb=False), _vote("a2", cb=False)), R6_HOLD),  # no callback
        ApprovalEvidence((_vote("a1"), _vote("a2"), _vote("a3", decision="reject")), R6_HOLD),
        ApprovalEvidence((_vote("a1"), _vote("a2"))),  # no rule outcomes: fail closed
    ]
    for ev in cases:
        with pytest.raises(IllegalTransition):
            transition(
                S.FLAGGED,
                S.READY_TO_PAY,
                actor=Actor.user("a2"),
                facts=FACTS,
                reason="t",
                evidence=ev,
            )
    for actor in (SYS, Actor.mcp("claude")):
        with pytest.raises(IllegalTransition):
            transition(S.FLAGGED, S.READY_TO_PAY, actor=actor, facts=FACTS, reason="t", evidence=ok)


def test_flagged_release_refused_while_review_failures_are_open() -> None:
    votes = (_vote("a1"), _vote("a2"))
    for rule in ("R9", "R5", "R3", "R7", "R8", "R2", "R1"):
        outs = [
            RuleOutcome(r, False, "flag", "bank") if r == "R6" else o
            for r, o in zip(ALL_RULE_IDS, with_fail(rule, "review"), strict=True)
        ]
        assert release_blockers(outs) == (f"{rule}: x",)
        with pytest.raises(IllegalTransition, match=f"release blocked.*{rule}: x"):
            transition(
                S.FLAGGED,
                S.READY_TO_PAY,
                actor=Actor.user("a2"),
                facts=FACTS,
                reason="t",
                evidence=ApprovalEvidence(votes, tuple(outs)),
            )
    # several flag-level holds (R6 + R4) with nothing else open: the approvers judge them
    both = [
        RuleOutcome(r, r not in ("R4", "R6"), "flag" if r in ("R4", "R6") else "pass")
        for r in ALL_RULE_IDS
    ]
    assert release_blockers(both) == ()
    transition(
        S.FLAGGED,
        S.READY_TO_PAY,
        actor=Actor.user("a2"),
        facts=FACTS,
        reason="t",
        evidence=ApprovalEvidence(votes, tuple(both)),
    )
    assert release_blockers(R6_HOLD[:-1]) == ("R10: rule result missing",)


def test_flagged_back_to_validating_only_by_a_human() -> None:
    rec = transition(S.FLAGGED, S.VALIDATING, actor=Actor.user("u"), facts=FACTS, reason="fix")
    assert rec.to_status is S.VALIDATING
    for actor in (SYS, Actor.mcp("c"), Actor("llm", "x")):
        with pytest.raises(IllegalTransition):
            transition(S.FLAGGED, S.VALIDATING, actor=actor, facts=FACTS, reason="fix")


def test_posted() -> None:
    rcpt = PostingReceipt("qbo", "123", "invoicedesk-bill-d1")
    rec = transition(
        S.READY_TO_PAY, S.POSTED, actor=Actor.mcp("c"), facts=FACTS, reason="t", evidence=rcpt
    )
    assert rec.details == {"system": "qbo", "external_id": "123"}
    with pytest.raises(IllegalTransition):
        transition(S.READY_TO_PAY, S.POSTED, actor=SYS, facts=FACTS, reason="t")
    with pytest.raises(IllegalTransition):
        transition(
            S.READY_TO_PAY,
            S.POSTED,
            actor=SYS,
            facts=FACTS,
            reason="t",
            evidence=PostingReceipt("qbo", "1", ""),
        )
    with pytest.raises(IllegalTransition):
        transition(S.READY_TO_PAY, S.POSTED, actor=SYS, facts=VISITOR, reason="t", evidence=rcpt)


def test_reject_and_review_loop() -> None:
    transition(S.FLAGGED, S.REJECTED, actor=Actor.user("u"), facts=FACTS, reason="t")
    with pytest.raises(IllegalTransition):
        transition(S.FLAGGED, S.REJECTED, actor=Actor.mcp("c"), facts=FACTS, reason="t")
    transition(S.NEEDS_REVIEW, S.VALIDATING, actor=Actor.user("u"), facts=FACTS, reason="fix")
    with pytest.raises(IllegalTransition):
        transition(S.NEEDS_REVIEW, S.VALIDATING, actor=SYS, facts=FACTS, reason="fix")


def test_kanban_and_actor_labels() -> None:
    assert kanban_column("Screening") == "received"
    assert kanban_column(S.QUARANTINED) == "flagged"
    assert set(map(kanban_column, S)) == {
        "received",
        "extracted",
        "validated",
        "ready_to_pay",
        "needs_review",
        "flagged",
        "posted",
        "rejected",
    }
    assert SYS.audit_label == "system"
    assert Actor.user("42").audit_label == "user:42"
    assert Actor.mcp("claude").audit_label == "mcp:claude"
    assert VISITOR.is_visitor_upload and not FACTS.is_visitor_upload
