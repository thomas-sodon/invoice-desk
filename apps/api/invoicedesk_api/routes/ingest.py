"""Ingest: ``POST /batches``, ``POST /documents`` (M1), ``POST /demo/run`` (US-8) and
``POST /ingest/email`` (M2, n8n)."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import email.utils
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from invoicedesk.accounting.demo_reset import DemoResetError
from invoicedesk.audit.chain import append as audit_append
from invoicedesk.contracts.preprocess import MAX_BYTES, RejectedInput
from invoicedesk.db import repo
from invoicedesk.masking import mask_text
from invoicedesk.status import Actor
from pydantic import ValidationError

from invoicedesk_api import limits
from invoicedesk_api import schemas as sc
from invoicedesk_api.access import batch_row
from invoicedesk_api.auth import (
    OptionalStaffDep,
    RuntimeDep,
    StaffDep,
    client_ip,
    ip_key,
    verify_ingest_signature,
)
from invoicedesk_api.demo import start_demo_run
from invoicedesk_api.errors import ApiError
from invoicedesk_api.uploads import (
    CheckedFile,
    check_upload,
    create_document,
    publish_created,
    read_limited,
    safe_filename,
)

router = APIRouter()

MAX_FILES_PER_REQUEST = 50
#: Batch modes a staff upload may join (``POST /batches`` creates only these).
UPLOAD_BATCH_MODES = frozenset({"live", "replay"})


@router.post("/batches", status_code=201, response_model=sc.BatchCreated)
def create_batch(
    *,
    body: sc.BatchCreate,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.BatchCreated:
    with rt.db.tx() as conn:
        bid = repo.create_batch(
            conn,
            tenant_id=rt.tenant,
            label=mask_text(body.label.strip()) or "",
            mode=body.mode,
            created_by=staff.id,
        )
    return sc.BatchCreated(batch_id=bid)


@router.post("/documents", status_code=202, response_model=sc.UploadResponse)
async def upload_documents(
    *,
    files: Annotated[list[UploadFile], File(alias="files[]")],
    batch_id: Annotated[str | None, Form()] = None,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.UploadResponse:
    if len(files) > MAX_FILES_PER_REQUEST:
        raise ApiError(422, "rejected_input", f"At most {MAX_FILES_PER_REQUEST} files at once.")
    checked: list[CheckedFile] = []
    rejected: list[sc.RejectedFile] = []
    for f in files:
        data = await read_limited(f)
        try:
            checked.append(check_upload(data, filename=f.filename, declared=f.content_type))
        except RejectedInput as exc:
            rejected.append(sc.RejectedFile(filename=safe_filename(f.filename), error=str(exc)))

    def _store() -> tuple[str, list[sc.UploadedDoc]]:
        with rt.db.tx() as conn:
            if batch_id:
                batch = batch_row(conn, rt, batch_id)
                if batch["mode"] not in UPLOAD_BATCH_MODES:
                    # Demo batches are publicly readable and wiped by the next demo run;
                    # try-it batches are purged after 24h. Staff uploads never go there.
                    raise ApiError(
                        409,
                        "illegal_batch",
                        "Invoices can only be uploaded to batches you created.",
                    )
                bid = str(batch["id"])
            else:
                bid = repo.create_batch(
                    conn, tenant_id=rt.tenant, label="Upload", mode="live", created_by=staff.id
                )
        docs: list[sc.UploadedDoc] = []
        for c in checked:
            doc_id = create_document(
                rt, c, source="upload", actor=staff.actor, batch_id=bid, uploaded_by=staff.id
            )
            rt.dispatch_process(doc_id)
            publish_created(rt, doc_id, bid)
            docs.append(sc.UploadedDoc(id=doc_id, filename=c.filename, status="Received"))
        return bid, docs

    bid, docs = await run_in_threadpool(_store)
    return sc.UploadResponse(batch_id=bid, documents=docs, rejected=rejected)


@router.post("/demo/run", status_code=202, response_model=sc.DemoRunResponse)
def demo_run(
    *,
    request: Request,
    body: sc.DemoRunRequest | None = None,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> sc.DemoRunResponse:
    """One click, no signup (US-8). Anonymous callers are rate-limited per IP and always get
    the recorded run (no LLM cost)."""
    requested = body.mode if body is not None else "replay"
    if staff is None:
        key = ip_key(rt, client_ip(request, rt))
        with rt.db.tx() as conn:
            ok = limits.take(
                conn,
                day=limits.today(rt),
                scope="ip_demo",
                key=key,
                limit=rt.api.tryit_demo_runs_per_ip_per_day,
            )
        if not ok:
            raise ApiError(429, "rate_limited", "Demo limit reached for today. Try tomorrow.")
    actor = staff.actor if staff is not None else Actor.system("tryit")
    try:
        run = start_demo_run(
            rt,
            requested=requested,
            actor=actor,
            uploaded_by=staff.id if staff is not None else None,
        )
    except DemoResetError as exc:
        raise ApiError(409, "demo_reset_required", str(exc)) from exc
    return sc.DemoRunResponse(batch_id=run.batch_id, document_ids=run.document_ids)


def _received_at(value: str | None) -> dt.datetime | None:
    """ISO 8601 or RFC 2822 (an IMAP ``Date`` header); anything else falls back to now().
    A bad date must not 500 the webhook: n8n would retry the delivery forever."""
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _decode_attachment(a: sc.EmailAttachment) -> bytes:
    if len(a.content_b64) > (MAX_BYTES * 4) // 3 + 8:
        raise RejectedInput(f"The file is over the {MAX_BYTES // 1_048_576} MB limit.")
    try:
        return base64.b64decode(a.content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RejectedInput("The attachment is not valid base64.") from exc


@router.post("/ingest/email", status_code=202, response_model=sc.EmailIngestResponse)
async def ingest_email(
    *, request: Request, rt: RuntimeDep
) -> sc.EmailIngestResponse | JSONResponse:
    """n8n IMAP trigger -> one record: the e-mail thread plus its attachments (M2). The body is
    stored with the thread and screened with every attached document (R6/R10 see it)."""
    raw = await request.body()
    verify_ingest_signature(rt, raw, request.headers.get("x-invoicedesk-signature"))
    try:
        msg = sc.EmailIngest.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise ApiError(422, "validation_error", "The e-mail payload is not valid.") from exc

    def _ingest() -> sc.EmailIngestResponse:
        actor = Actor.system("email_ingest")
        with rt.db.tx() as conn:
            row = conn.execute(
                "insert into email_threads (tenant_id, message_id, from_addr, subject, body_text,"
                " received_at) values (%s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))"
                " on conflict (tenant_id, message_id) do nothing returning id",
                (
                    rt.tenant,
                    msg.message_id,
                    msg.from_,
                    msg.subject,
                    msg.body_text,
                    _received_at(msg.received_at),
                ),
            ).fetchone()
            if row is None:  # n8n retried a delivery we already have: same record back
                existing = conn.execute(
                    "select id from email_threads where tenant_id = %s and message_id = %s",
                    (rt.tenant, msg.message_id),
                ).fetchone()
                assert existing is not None
                tid = str(existing["id"])
                docs = conn.execute(
                    "select d.id, d.filename from document_threads dt join documents d"
                    " on d.id = dt.document_id where dt.thread_id = %s order by d.created_at",
                    (tid,),
                ).fetchall()
                if docs or not msg.attachments:
                    return sc.EmailIngestResponse(
                        thread_id=tid,
                        documents=[
                            sc.EmailDoc(id=str(d["id"]), filename=d["filename"] or "") for d in docs
                        ],
                    )
                # an earlier delivery stopped before its attachments were stored: finish it
            else:
                tid = str(row["id"])
            audit_append(
                conn,
                actor=actor.audit_label,
                action="email.received",
                tenant_id=rt.tenant,
                after={
                    "thread_id": tid,
                    "from": msg.from_,
                    "subject": mask_text(msg.subject),
                    "attachments": len(msg.attachments),
                },
            )
        out: list[sc.EmailDoc] = []
        rejected: list[sc.RejectedFile] = []
        for a in msg.attachments:
            try:
                f = check_upload(
                    _decode_attachment(a), filename=a.filename, declared=a.content_type
                )
            except RejectedInput as exc:
                rejected.append(sc.RejectedFile(filename=safe_filename(a.filename), error=str(exc)))
                continue
            doc_id = create_document(rt, f, source="email", actor=actor, thread_id=tid)
            rt.dispatch_process(doc_id)
            publish_created(rt, doc_id, None)
            out.append(sc.EmailDoc(id=doc_id, filename=f.filename))
        return sc.EmailIngestResponse(thread_id=tid, documents=out, rejected=rejected)

    return await run_in_threadpool(_ingest)
