"""Release votes are scoped: right role, right kind (R6), cast during the current hold."""

from __future__ import annotations

from typing import Any

import pytest
from invoicedesk.pipeline.graph import PipelineRunner
from invoicedesk.pipeline.services import NotAnApprover, PipelineServices
from invoicedesk.testing.builders import make_extraction, money, text

pytestmark = pytest.mark.postgres


def _bank_change_doc(world: Any) -> str:
    return world.add_document(
        make_extraction(
            invoice_number=text("INV-7001"), remit_to_bank_details=text(world.ATTACKER_REMIT)
        )
    )


def test_unrelated_kind_vote_cannot_stand_in_for_bank_change_attestation(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    doc = _bank_change_doc(world)
    assert runner.process(doc)["status"] == "Flagged"
    # Approver A only "approves" something else, without a callback.
    first = services.record_approval(
        doc, kind="other", approver=world.approver_a, decision="approve", callback_attested=False
    )
    assert not first.released
    # Approver B attests the bank change: still only ONE bank-change approver.
    second = services.record_approval(
        doc,
        kind="bank_change",
        approver=world.approver_b,
        decision="approve",
        callback_attested=True,
    )
    assert not second.released and world.status(doc) == "Flagged"
    # A's own bank-change attestation completes the two-person rule.
    third = services.record_approval(
        doc,
        kind="bank_change",
        approver=world.approver_a,
        decision="approve",
        callback_attested=True,
    )
    assert third.released and world.status(doc) == "ReadyToPay"
    assert world.audit_ok()


def test_vote_cast_before_the_hold_does_not_count(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    # Low-confidence total -> NeedsReview (R9).
    doc = world.add_document(
        make_extraction(invoice_number=text("INV-7002"), total=money(124_000, conf=0.3))
    )
    assert runner.process(doc)["status"] == "NeedsReview"
    # A pre-emptive vote while in NeedsReview does nothing...
    early = services.record_approval(
        doc, kind="duplicate", approver=world.approver_a, decision="approve", callback_attested=True
    )
    assert not early.released and not early.revalidated
    # ...a correction turns it into a duplicate of INV-2231 -> Flagged (R4).
    out = services.apply_corrections(
        doc,
        [{"field": "invoice_number", "value": "INV-2231"}, {"field": "total", "value": "124000"}],
        world.clerk,
    )
    assert out.status == "Flagged"
    # One approver during the hold is not two people, whatever was recorded earlier.
    one = services.record_approval(
        doc, kind="duplicate", approver=world.approver_b, decision="approve", callback_attested=True
    )
    assert not one.released and world.status(doc) == "Flagged"
    # A fresh vote from A during the hold completes it.
    two = services.record_approval(
        doc, kind="other", approver=world.approver_a, decision="approve", callback_attested=False
    )
    assert two.released and world.status(doc) == "ReadyToPay"


def test_clerks_cannot_vote(world: Any, runner: PipelineRunner, services: PipelineServices) -> None:
    from invoicedesk.db import repo

    with world.db.tx() as conn:
        other_clerk = repo.create_staff_user(
            conn, tenant_id="maple_row", email="cleo@maplerow.test", display_name="Cleo Clerk"
        )
    doc = _bank_change_doc(world)
    assert runner.process(doc)["status"] == "Flagged"
    with pytest.raises(NotAnApprover):
        services.record_approval(
            doc,
            kind="bank_change",
            approver=other_clerk,
            decision="approve",
            callback_attested=True,
        )
    # Unknown staff ids are refused too.
    with pytest.raises(NotAnApprover):
        services.record_approval(
            doc,
            kind="bank_change",
            approver="00000000-0000-0000-0000-000000000000",
            decision="approve",
            callback_attested=True,
        )
    assert world.status(doc) == "Flagged"
    assert world.count("approvals", doc) == 0


def test_visitor_correction_writes_no_eval_example(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    doc = world.add_document(
        make_extraction(invoice_number=text("INV-7003"), total=money(124_000, conf=0.3)),
        session_id="sess-eval",
        uploaded_by=None,
    )
    assert runner.process(doc)["status"] == "NeedsReview"
    services.apply_corrections(doc, [{"field": "total", "value": "124000"}], world.clerk)
    assert world.status(doc) == "ReadyToPay"
    assert world.count("reviews", doc) == 1
    # Purged after 24h, so nothing of it may persist in the eval set.
    assert world.count("eval_examples", doc) == 0


def test_comparison_grades_a_two_person_release_as_held_not_leaked(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    from invoicedesk.pipeline.naive import compare_batch

    batch = world.batch("release")
    doc = world.add_document(
        make_extraction(
            invoice_number=text("INV-7004"), remit_to_bank_details=text(world.ATTACKER_REMIT)
        ),
        batch_id=batch,
    )
    assert runner.process(doc)["status"] == "Flagged"
    for approver in (world.approver_a, world.approver_b):
        services.record_approval(
            doc, kind="bank_change", approver=approver, decision="approve", callback_attested=True
        )
    assert world.status(doc) == "ReadyToPay"
    out = compare_batch(world.db, batch)
    mine = out["rows"][0]["invoicedesk"]
    assert mine["verdict"] == "correct" and "two-person" in mine["reason"]
    assert out["summary"]["invoicedesk_wrong"] == 0


# ---------------------------------------------------------------- ADR-0008: fix, then vote
def _vote(services: PipelineServices, doc: str, who: str, kind: str = "bank_change") -> Any:
    return services.record_approval(
        doc, kind=kind, approver=who, decision="approve", callback_attested=True
    )


def _failed(world: Any, doc: str) -> dict[str, str]:
    with world.db.tx() as conn:
        rows = conn.execute(
            "select rule_id, severity from rule_results where document_id = %s and not passed"
            " and evaluation = (select max(evaluation) from rule_results where document_id = %s)",
            (doc, doc),
        ).fetchall()
    return {r["rule_id"]: r["severity"] for r in rows}


def test_bank_change_with_low_confidence_total_is_not_released_unverified(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    from invoicedesk.pipeline.services import ReleaseBlocked

    doc = world.add_document(
        make_extraction(
            invoice_number=text("INV-7101"),
            total=money(124_000, conf=0.3),
            remit_to_bank_details=text(world.ATTACKER_REMIT),
        )
    )
    assert runner.process(doc)["status"] == "Flagged"
    assert _failed(world, doc) == {"R6": "flag", "R9": "review"}
    # Two approvers with a callback are not enough while the total is unverified.
    with pytest.raises(ReleaseBlocked, match="R9"):
        _vote(services, doc, world.approver_a)
    assert world.count("approvals", doc) == 0 and world.status(doc) == "Flagged"
    # The reviewer confirms the total on the held invoice; R6 holds it again (a new hold).
    out = services.apply_corrections(doc, [{"field": "total", "value": "124000"}], world.clerk)
    assert out.status == "Flagged"
    assert _failed(world, doc) == {"R6": "flag"}
    first = _vote(services, doc, world.approver_a)
    assert not first.released
    # Once a release vote is cast, a correction would void it: refused.
    from invoicedesk.status import IllegalTransition

    with pytest.raises(IllegalTransition, match="already voted"):
        services.apply_corrections(doc, [{"field": "total", "value": "124001"}], world.clerk)
    second = _vote(services, doc, world.approver_b)
    assert second.released and world.status(doc) == "ReadyToPay"
    assert world.audit_ok()


def test_bank_change_over_cap_clears_r7_then_votes_on_the_new_hold(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    from invoicedesk.pipeline.services import ReleaseBlocked

    doc = world.add_document(
        make_extraction(
            invoice_number=text("INV-7102"),
            total=money(640_000),
            subtotal=money(640_000),
            remit_to_bank_details=text(world.ATTACKER_REMIT),
        )
    )
    assert runner.process(doc)["status"] == "Flagged"
    failed = _failed(world, doc)
    assert failed.get("R6") == "flag" and failed.get("R7") == "review"
    with pytest.raises(ReleaseBlocked, match="R7"):
        _vote(services, doc, world.approver_a)
    # The over-cap approval is read by R7 on re-validation; R6 flags it again.
    res = _vote(services, doc, world.approver_a, kind="over_cap")
    assert res.revalidated and not res.released and world.status(doc) == "Flagged"
    failed = _failed(world, doc)
    assert "R7" not in failed and failed["R6"] == "flag"
    # The line items still do not add up to the new total (R5) and it is over the estimate
    # (R3): no two-person vote is accepted until those are fixed or the invoice is rejected.
    assert {k for k, v in failed.items() if v != "flag"} >= {"R3", "R5"}
    with pytest.raises(ReleaseBlocked, match="R3"):
        _vote(services, doc, world.approver_b)
    assert world.status(doc) == "Flagged" and world.count("approvals", doc) == 1


def test_quarantined_document_cannot_be_corrected(
    world: Any, runner: PipelineRunner, services: PipelineServices
) -> None:
    from invoicedesk.status import IllegalTransition

    doc = _bank_change_doc(world)
    assert runner.process(doc)["status"] == "Flagged"
    with world.db.tx() as conn:  # simulate a never-extracted (quarantined) Flagged document
        conn.execute(
            "delete from pipeline_steps where document_id = %s and step = 'extract'", (doc,)
        )
    with pytest.raises(IllegalTransition, match="never read"):
        services.apply_corrections(doc, [{"field": "total", "value": "124000"}], world.clerk)
