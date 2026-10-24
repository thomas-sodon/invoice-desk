"""Agent facade for the MCP server (M16, US-13; DESIGN §5.5 MCP tools).

Invariant 2: components with tools never receive raw document text. These responses carry
field *values* only (masked), flags, and a *count* of embedded instructions: never OCR text,
screening spans, the instructions themselves or bank details. Posting needs ``ReadyToPay``
(always refused for ``Flagged``) plus a human confirmation that matches the stored vendor and
total; the audit actor is ``mcp:<client>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from invoicedesk.contracts.accounting import AccountingError
from invoicedesk.contracts.extraction import MONEY_FIELDS
from invoicedesk.contracts.preprocess import RejectedInput
from invoicedesk.masking import mask_text
from invoicedesk.pipeline.steps import load_step_conn
from invoicedesk.status import DocStatus

from invoicedesk_api import schemas as sc
from invoicedesk_api.access import doc_row
from invoicedesk_api.auth import AgentPostDep, AgentReadDep, AgentWriteDep, RuntimeDep
from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime
from invoicedesk_api.uploads import check_upload, create_document, read_limited, safe_filename
from invoicedesk_api.views import build_cards, latest_rule_rows, rule_result_out

router = APIRouter(prefix="/agent")

EXTRACT_TIMEOUT_S = 60.0
_NOT_FOR_AGENTS = frozenset({"vendor_id", "remit_to_bank_details"})


def _handle_prefix(rt: Runtime) -> str:
    return f"{rt.tenant}/agent-uploads/"


@router.post("/uploads", status_code=201, response_model=sc.AgentUploadResponse)
async def agent_upload(
    *,
    file: Annotated[UploadFile, File()],
    rt: RuntimeDep,
    agent: AgentWriteDep,
) -> sc.AgentUploadResponse:
    data = await read_limited(file)
    checked = check_upload(data, filename=file.filename, declared=file.content_type)
    key = f"{_handle_prefix(rt)}{uuid.uuid4().hex}/{checked.filename}"
    await run_in_threadpool(rt.storage.put, key, checked.data, content_type=checked.mime)
    return sc.AgentUploadResponse(upload_handle=key)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def _extract_response(rt: Runtime, doc_id: str) -> sc.AgentExtractResponse:
    with rt.db.tx() as conn:
        doc = doc_row(conn, rt, doc_id)
        rows = conn.execute(
            "select name, value, calibrated_conf from fields where document_id = %s and final",
            (doc_id,),
        ).fetchall()
        card = build_cards(conn, rt, [doc])[0]
        ext = load_step_conn(conn, doc_id, "extract") or {}
    instructions = (ext.get("extraction") or {}).get("embedded_instructions") or []
    return sc.AgentExtractResponse(
        document_id=doc_id,
        status=str(doc["status"]),
        fields={
            r["name"]: sc.AgentField(
                value=r["value"]
                if r["name"] in MONEY_FIELDS and (r["value"] or "").lstrip("-").isdigit()
                else mask_text(r["value"]),
                calibrated_confidence=r["calibrated_conf"],
            )
            for r in rows
            if r["name"] not in _NOT_FOR_AGENTS
        },
        flags=card.badges,
        embedded_instructions=len(instructions),
    )


@router.post("/extract", response_model=sc.AgentExtractResponse)
async def agent_extract(
    *,
    body: sc.AgentExtractRequest,
    rt: RuntimeDep,
    agent: AgentWriteDep,
) -> sc.AgentExtractResponse:
    if (body.file_url is None) == (body.upload_handle is None):
        raise ApiError(422, "validation_error", "Give exactly one of file_url or upload_handle.")

    def _load() -> tuple[bytes, str, str | None]:
        if body.upload_handle is not None:
            key = body.upload_handle
            if not key.startswith(_handle_prefix(rt)) or ".." in key:
                raise ApiError(404, "not_found", "Unknown upload_handle.")
            try:
                return rt.storage.get(key), key.rsplit("/", 1)[-1], None
            except (KeyError, FileNotFoundError, OSError) as exc:
                raise ApiError(404, "not_found", "Unknown upload_handle.") from exc
        assert body.file_url is not None
        data, ctype = rt.fetcher.fetch(body.file_url)
        return data, safe_filename(body.file_url.split("?")[0].rsplit("/", 1)[-1]), ctype

    data, filename, ctype = await run_in_threadpool(_load)
    try:
        checked = check_upload(data, filename=filename, declared=ctype)
    except RejectedInput:
        raise

    def _create() -> str:
        return create_document(rt, checked, source="mcp", actor=agent.actor)

    doc_id = await run_in_threadpool(_create)

    def _run() -> None:
        rt.services_for(doc_id).process(doc_id)

    # On timeout the run continues; the response reports the status reached so far.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(run_in_threadpool(_run), timeout=EXTRACT_TIMEOUT_S)
    return await run_in_threadpool(_extract_response, rt, doc_id)


@router.get("/documents/{document_id}/validation", response_model=sc.AgentValidation)
def agent_validation(
    *,
    document_id: str,
    rt: RuntimeDep,
    agent: AgentReadDep,
) -> sc.AgentValidation:
    with rt.db.tx() as conn:
        doc = doc_row(conn, rt, document_id)
        if doc["session_id"] is not None:
            raise ApiError(404, "not_found", "No such document.")
        rows = latest_rule_rows(conn, [str(doc["id"])])[str(doc["id"])]
    variance = [
        mask_text(str((r["details_json"] or {}).get("variance_sentence") or r["message"])) or ""
        for r in rows
        if r["rule_id"] == "R3" and not r["passed"]
    ]
    return sc.AgentValidation(
        status=str(doc["status"]),
        rule_results=[rule_result_out(r) for r in rows],
        variance_explanations=variance,
    )


@router.post("/documents/{document_id}/post-bill", response_model=sc.AgentPostResponse)
def agent_post_bill(
    *,
    document_id: str,
    body: sc.AgentPostRequest,
    rt: RuntimeDep,
    agent: AgentPostDep,
) -> sc.AgentPostResponse:
    with rt.db.tx() as conn:
        doc = doc_row(conn, rt, document_id)
        doc_id = str(doc["id"])
        fields = {
            r["name"]: r["value"]
            for r in conn.execute(
                "select name, value from fields where document_id = %s and final", (doc_id,)
            ).fetchall()
        }
        legal = None
        if fields.get("vendor_id"):
            v = conn.execute(
                "select legal_name from vendors where id::text = %s", (fields["vendor_id"],)
            ).fetchone()
            legal = None if v is None else v["legal_name"]
        bill = conn.execute(
            "select system, external_id from bills where document_id = %s and status = 'posted'"
            " order by created_at desc limit 1",
            (doc_id,),
        ).fetchone()
    status = DocStatus(doc["status"])
    if doc["session_id"] is not None:
        raise ApiError(409, "illegal_transition", "Try-it uploads are never posted.")
    if status is DocStatus.FLAGGED:
        raise ApiError(
            409,
            "illegal_transition",
            "Flagged invoices need two human approvers; agents cannot post them.",
        )
    if status not in (DocStatus.READY_TO_PAY, DocStatus.POSTED):
        raise ApiError(
            409, "illegal_transition", f"Only Ready to pay invoices can be posted (is {status})."
        )
    c = body.confirmation
    if not c.confirmed_by_human:
        raise ApiError(422, "confirmation_required", "A human must confirm the vendor and amount.")
    total = fields.get("total")
    vendor_ok = _norm(c.vendor_name) in {_norm(fields.get("vendor_name")), _norm(legal)} - {""}
    if not vendor_ok or total is None or str(c.total_cents) != str(total):
        raise ApiError(
            422, "confirmation_mismatch", "The confirmed vendor or total differs from the invoice."
        )
    if status is DocStatus.POSTED and bill is not None:
        return sc.AgentPostResponse(
            bill_id=str(bill["external_id"]), system=bill["system"], status="Posted"
        )
    try:
        res = rt.services_for(doc_id).post_document(doc_id, actor=agent.actor)
    except AccountingError as exc:
        raise ApiError(
            502, "accounting_error", "The accounting system did not accept the bill."
        ) from exc
    return sc.AgentPostResponse(bill_id=res.bill_id, system=res.system, status="Posted")
