"""Human actions (staff): corrections (M10), approvals (US-5), reject, post (M11) and vendor
follow-ups (M15). Every state change goes through ``invoicedesk.pipeline.services`` and so
through ``status.transition`` with its evidence gates; this layer only authenticates, checks
roles and shapes responses."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query
from invoicedesk.audit.chain import append as audit_append
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.contracts.tenant_data import Vendor
from invoicedesk.extract.wording import draft_followup
from invoicedesk.masking import mask_text
from invoicedesk.status import DocStatus

from invoicedesk_api import schemas as sc
from invoicedesk_api.access import batch_row, doc_row, parse_uuid
from invoicedesk_api.auth import ApproverDep, RuntimeDep, StaffDep
from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime
from invoicedesk_api.views import approvals_for, followup_out, latest_rule_rows

router = APIRouter()
log = logging.getLogger("invoicedesk.api.actions")


def _require_doc(rt: Runtime, document_id: str) -> dict[str, Any]:
    with rt.db.tx() as conn:
        return doc_row(conn, rt, document_id)


# ---------------------------------------------------------------------------- corrections
@router.post("/documents/{document_id}/corrections", response_model=sc.StatusOut)
def correct(
    *,
    document_id: str,
    body: sc.CorrectionsRequest,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.StatusOut:
    doc = _require_doc(rt, document_id)
    if doc["session_id"] is not None:
        raise ApiError(403, "forbidden", "Try-it uploads are read-only.")
    svc = rt.services_for(str(doc["id"]))
    res = svc.apply_corrections(
        str(doc["id"]),
        [{"field": c.field, "value": c.value} for c in body.corrections],
        reviewer=staff.id,
    )
    return sc.StatusOut(status=res.status)


# ---------------------------------------------------------------------------- approvals
@router.post("/documents/{document_id}/approvals", response_model=sc.ApprovalResponse)
def approve(
    *,
    document_id: str,
    body: sc.ApprovalRequest,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.ApprovalResponse:
    doc = _require_doc(rt, document_id)
    doc_id = str(doc["id"])
    # Self-approval is checked first so the uploader always gets the specific 403.
    if doc["uploaded_by"] is not None and str(doc["uploaded_by"]) == staff.id:
        raise ApiError(403, "self_approval", "You uploaded this invoice, so you cannot approve it.")
    if not staff.can_approve:
        raise ApiError(403, "forbidden", "Only an approver or owner can vote on a held invoice.")
    if doc["session_id"] is not None:
        raise ApiError(403, "forbidden", "Try-it uploads are never released or posted.")
    rt.services_for(doc_id).record_approval(
        doc_id,
        kind=body.kind,
        approver=staff.actor,
        decision=body.decision,
        callback_attested=body.callback_attested,
        note=mask_text(body.note),
    )
    with rt.db.tx() as conn:
        approvals = approvals_for(conn, doc_id)
        status = str(doc_row(conn, rt, doc_id)["status"])
    return sc.ApprovalResponse(approvals=approvals, status=status)


# ---------------------------------------------------------------------------- reject
@router.post("/documents/{document_id}/reject", response_model=sc.StatusOut)
def reject(
    *,
    document_id: str,
    body: sc.RejectRequest | None = None,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.StatusOut:
    doc = _require_doc(rt, document_id)
    reason = mask_text((body.reason if body else "").strip()) or "Rejected by reviewer"
    status = rt.services_for(str(doc["id"])).reject(
        str(doc["id"]), actor=staff.actor, reason=reason
    )
    return sc.StatusOut(status=status)


# ---------------------------------------------------------------------------- posting
def _check_postable(doc: dict[str, Any]) -> None:
    if doc["session_id"] is not None:
        raise ApiError(403, "forbidden", "Try-it uploads are never posted to accounting.")
    if doc["status"] != DocStatus.READY_TO_PAY.value:
        raise ApiError(
            409,
            "illegal_transition",
            f"Only Ready to pay invoices can be posted (is {doc['status']}).",
        )


@router.post("/documents/{document_id}/post", status_code=202, response_model=sc.JobOut)
def post(
    *,
    document_id: str,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.JobOut:
    doc = _require_doc(rt, document_id)
    _check_postable(doc)
    job = rt.dispatch_post(str(doc["id"]), actor_label=staff.actor.audit_label)
    return sc.JobOut(job_id=str(job))


@router.post("/batches/{batch_id}/post-ready", status_code=202, response_model=sc.PostReadyOut)
def post_ready(
    *,
    batch_id: str,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.PostReadyOut:
    with rt.db.tx() as conn:
        bid = str(batch_row(conn, rt, batch_id)["id"])
    ids = rt.services().ready_documents(bid)
    jobs = [str(rt.dispatch_post(d, actor_label=staff.actor.audit_label)) for d in ids]
    return sc.PostReadyOut(job_ids=jobs, count=len(jobs))


# ---------------------------------------------------------------------------- follow-ups
def _issues(rows: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for r in rows:
        if not r["passed"]:
            details = r["details_json"] or {}
            head = details.get("headline")
            issues.append(str(head if isinstance(head, str) and head else r["message"]))
    return issues


@router.post("/documents/{document_id}/followups", status_code=201, response_model=sc.Followup)
def create_followup(
    *,
    document_id: str,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.Followup:
    with rt.db.tx() as conn:
        doc = doc_row(conn, rt, document_id)
        doc_id = str(doc["id"])
        if doc["session_id"] is not None:
            raise ApiError(403, "forbidden", "Try-it uploads get no vendor follow-ups.")
        fields = {
            r["name"]: r["value"]
            for r in conn.execute(
                "select name, value from fields where document_id = %s and final", (doc_id,)
            ).fetchall()
        }
        vendor_id = fields.get("vendor_id")
        vrow = (
            conn.execute(
                "select * from vendors where id::text = %s and tenant_id = %s",
                (vendor_id, rt.tenant),
            ).fetchone()
            if vendor_id
            else None
        )
        issues = _issues(latest_rule_rows(conn, [doc_id])[doc_id])
    if vrow is None:
        raise ApiError(
            422,
            "no_vendor",
            "This invoice's vendor is not in the vendor list, so there is no"
            " address on file to write to.",
        )
    if not vrow["email"]:
        raise ApiError(422, "no_vendor_email", "The vendor list has no e-mail for this vendor.")
    if not issues:
        raise ApiError(409, "nothing_to_ask", "All checks passed; there is nothing to ask for.")
    vendor = Vendor(
        id=str(vrow["id"]),
        legal_name=vrow["legal_name"],
        aliases=tuple(vrow["aliases"] or ()),
        address=vrow["address"],
        tax_id_last4=vrow["tax_id_last4"],
        remit_to_hash=vrow["remit_to_hash"],
        bank_last4=vrow["bank_last4"],
        phone_on_file=vrow["phone_on_file"],
        email=vrow["email"],
        status=vrow["status"],
    )
    draft = draft_followup(
        rt.wording_llm, vendor=vendor, issues=issues, invoice_number=fields.get("invoice_number")
    )
    with rt.db.tx() as conn:
        row = conn.execute(
            "insert into followups (document_id, to_addr, draft_subject, draft_body)"
            " values (%s, %s, %s, %s) returning *",
            (doc_id, vendor.email, draft.subject, draft.body),
        ).fetchone()
        assert row is not None
        audit_append(
            conn,
            actor=staff.actor.audit_label,
            action="followup.drafted",
            document_id=doc_id,
            tenant_id=rt.tenant,
            after={
                "followup_id": str(row["id"]),
                "to": vendor.email,
                "source": draft.source,
                "words": draft.word_count,
            },
        )
    try:
        rt.publisher.publish(
            BoardEvent(
                type="followup.drafted",
                tenant_id=rt.tenant,
                document_id=doc_id,
                batch_id=None if doc["batch_id"] is None else str(doc["batch_id"]),
                status=str(doc["status"]),
                message=mask_text(draft.subject),
            )
        )
    except Exception:
        log.warning("followup.drafted publish failed", exc_info=True)
    return followup_out(dict(row))


@router.get("/followups", response_model=sc.FollowupList)
def list_followups(
    *,
    status: Annotated[str | None, Query(pattern="^(draft|approved|sent)$")] = None,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.FollowupList:
    base = (
        "select f.* from followups f join documents d on d.id = f.document_id"
        " where d.tenant_id = %s and (%s::text is null"
        " or (%s = 'draft' and f.approved_by is null)"
        " or (%s = 'approved' and f.approved_by is not null and f.sent_at is null)"
        " or (%s = 'sent' and f.sent_at is not null)) order by f.created_at desc, f.id"
    )
    with rt.db.tx() as conn:
        rows = conn.execute(base, (rt.tenant, status, status, status, status)).fetchall()
    return sc.FollowupList(items=[followup_out(dict(r)) for r in rows])


def _send_followup(rt: Runtime, row: dict[str, Any]) -> bool:
    """POST the approved follow-up to the n8n send webhook (Mailpit in dev). No webhook URL
    (the offline default) means nothing is sent."""
    url = rt.api.n8n_followup_webhook_url
    if not url:
        return False
    payload = {
        "followup_id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "to": row["to_addr"],
        "subject": row["draft_subject"],
        "body": mask_text(row["draft_body"]),
    }
    try:
        resp = rt.http.post(url, json=payload, timeout=15.0)
    except Exception as exc:
        log.warning("follow-up webhook failed: %s", type(exc).__name__)
        return False
    if resp.status_code >= 300:
        log.warning("follow-up webhook returned %s", resp.status_code)
        return False
    return True


@router.post("/followups/{followup_id}/approve", response_model=sc.Followup)
def approve_followup(
    *,
    followup_id: str,
    body: sc.FollowupApprove | None = None,
    rt: RuntimeDep,
    staff: ApproverDep,
) -> sc.Followup:
    fid = parse_uuid(followup_id, "follow-up")
    edits = body or sc.FollowupApprove(subject=None, body=None)
    with rt.db.tx() as conn:
        row = conn.execute(
            "select f.* from followups f join documents d on d.id = f.document_id"
            " where f.id = %s and d.tenant_id = %s for update of f",
            (fid, rt.tenant),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "No such follow-up.")
        if row["sent_at"] is not None:
            raise ApiError(409, "already_sent", "This follow-up was already sent.")
        subject = mask_text(edits.subject.strip()) if edits.subject else row["draft_subject"]
        text = mask_text(edits.body.strip()) if edits.body else row["draft_body"]
        if not subject or not text:
            raise ApiError(422, "validation_error", "Subject and body cannot be empty.")
        row = conn.execute(
            "update followups set draft_subject = %s, draft_body = %s, approved_by = %s,"
            " approved_at = coalesce(approved_at, now()) where id = %s returning *",
            (subject, text, staff.id, fid),
        ).fetchone()
        assert row is not None
        audit_append(
            conn,
            actor=staff.actor.audit_label,
            action="followup.approved",
            document_id=str(row["document_id"]),
            tenant_id=rt.tenant,
            after={"followup_id": fid, "edited": bool(edits.subject or edits.body)},
        )
    if _send_followup(rt, dict(row)):
        with rt.db.tx() as conn:
            row = conn.execute(
                "update followups set sent_at = coalesce(sent_at, now()) where id = %s returning *",
                (fid,),
            ).fetchone()
            assert row is not None
            audit_append(
                conn,
                actor="system",
                action="followup.sent",
                document_id=str(row["document_id"]),
                tenant_id=rt.tenant,
                after={"followup_id": fid, "to": row["to_addr"]},
            )
    return followup_out(dict(row))
