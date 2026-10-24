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
