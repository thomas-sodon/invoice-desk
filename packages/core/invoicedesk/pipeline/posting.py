"""Posting a ReadyToPay document (M11 caller side; CLAUDE.md invariants 2 and 4).

The :class:`~invoicedesk.contracts.accounting.BillRequest` is built from *final, validated
fields* and the *vendor master* only: the vendor reference comes from ``vendors.qbo_vendor_ref``
(never the printed vendor name), the line description from the work order on file, and bank or
remit-to details never appear (the request model forbids them anyway). Every call carries the
deterministic idempotency key, and the ``bills`` row is upserted by it, so a retried post can
never create a second bill.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

from invoicedesk.contracts.accounting import (
    AccountingAdapter,
    AccountingError,
    BillLine,
    BillRequest,
    idempotency_key_for,
)
from invoicedesk.contracts.infra import Storage
from invoicedesk.db import repo
from invoicedesk.db.connection import Conn, Database
from invoicedesk.rules._common import find_work_order
from invoicedesk.status import Actor, DocStatus, IllegalTransition, PostingReceipt

__all__ = [
    "PostResult",
    "PostingError",
    "build_bill_request",
    "final_fields",
    "post_document",
]

_DOC_NUMBER_RE = re.compile(r"[^A-Za-z0-9\-_/.#]")


class PostingError(ValueError):
    """The document cannot be turned into a bill (missing vendor mapping, total, date...)."""


@dataclass(frozen=True, slots=True)
class PostResult:
    document_id: str
    bill_id: str
    system: Literal["qbo", "xero", "fake"]
    status: Literal["Posted"]
    already_posted: bool
    attachment_id: str | None = None


def final_fields(conn: Conn, document_id: str) -> dict[str, str | None]:
    rows = conn.execute(
        "select name, value from fields where document_id = %s and final", (document_id,)
    ).fetchall()
    return {r["name"]: r["value"] for r in rows}


def _int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def build_bill_request(
    conn: Conn, doc: repo.DocumentRow, *, expense_account_ref: str
) -> BillRequest:
    f = final_fields(conn, doc.id)
    vendor_id = f.get("vendor_id")
    if not vendor_id:
        raise PostingError("no resolved vendor; cannot post")
    vrow = conn.execute(
        "select qbo_vendor_ref, status from vendors where id = %s and tenant_id = %s",
        (vendor_id, doc.tenant_id),
    ).fetchone()
    if vrow is None or not vrow["qbo_vendor_ref"]:
        raise PostingError("vendor has no accounting reference on file")
    if vrow["status"] != "approved":
        raise PostingError("vendor is not approved")
    total = _int(f.get("total"))
    if total is None or total <= 0:
        raise PostingError("no positive total")
    doc_number = _DOC_NUMBER_RE.sub("", f.get("invoice_number") or "")[:21]
    if not doc_number:
        raise PostingError("no invoice number")
    try:
        txn_date = dt.date.fromisoformat(f.get("invoice_date") or "")
    except ValueError as exc:
        raise PostingError("no invoice date") from exc
    due: dt.date | None
    try:
        due_raw = f.get("due_date")
        due = dt.date.fromisoformat(due_raw) if due_raw else None
    except ValueError:
        due = None

    tenant = repo.load_tenant_data(conn, doc.tenant_id, exclude_document_id=doc.id)
    wo = find_work_order(tenant, f.get("work_order_number"))
    memo: str | None = None
    description = f"Invoice {doc_number}"
    if wo is not None:
        unit = tenant.unit(wo.unit_id)
        memo = f"{wo.wo_number} Unit {unit.label}" if unit else wo.wo_number
        description = f"{wo.wo_number}: {wo.scope}"[:4000]

    subtotal, tax = _int(f.get("subtotal")), _int(f.get("tax"))
    if subtotal is not None and tax is not None and tax > 0 and subtotal + tax == total:
        lines = (
            BillLine(
                description=description,
                amount_cents=subtotal,
                expense_account_ref=expense_account_ref,
            ),
            BillLine(
                description="Sales tax", amount_cents=tax, expense_account_ref=expense_account_ref
            ),
        )
    else:
        lines = (
            BillLine(
                description=description, amount_cents=total, expense_account_ref=expense_account_ref
            ),
        )
    return BillRequest(
        vendor_ref=str(vrow["qbo_vendor_ref"]),
        doc_number=doc_number,
        txn_date=txn_date,
        due_date=due,
        lines=lines,
        total_cents=total,
        memo=memo,
    )


def _existing_bill(conn: Conn, key: str) -> dict[str, object] | None:
    row = conn.execute(
        "select system, external_id, attachment_id, status from bills where idempotency_key = %s",
        (key,),
    ).fetchone()
    return None if row is None else dict(row)


def post_document(
    db: Database,
    document_id: str,
    adapter: AccountingAdapter,
    storage: Storage,
    *,
    actor: Actor | None = None,
    expense_account_ref: str,
) -> PostResult:
    """Create the bill and attach the original. Idempotent: safe to call any number of times."""
    actor = actor or Actor.system("posting")
    bill_key = idempotency_key_for(document_id, "bill")
    attach_key = idempotency_key_for(document_id, "attach")

    with db.tx() as conn:
        doc = repo.get_document(conn, document_id, for_update=True)
        if doc.status is DocStatus.POSTED:
            existing = _existing_bill(conn, bill_key)
            if existing is None or existing["external_id"] is None:
                raise PostingError("document is Posted but has no bill row")
            return PostResult(
                document_id,
                str(existing["external_id"]),
                adapter.system,
                "Posted",
                already_posted=True,
                attachment_id=None
                if existing["attachment_id"] is None
                else str(existing["attachment_id"]),
            )
        facts = repo.document_facts(doc)
        if facts.is_visitor_upload:
            raise IllegalTransition("visitor (try-it) uploads are never posted")
        if actor.kind == "llm":
            raise IllegalTransition("LLM output can never post a bill")
        if doc.status is not DocStatus.READY_TO_PAY:
            raise IllegalTransition(f"only ReadyToPay documents can be posted (is {doc.status})")
        bill = build_bill_request(conn, doc, expense_account_ref=expense_account_ref)
        if _existing_bill(conn, bill_key) is None:
            repo.record_bill(
                conn,
                document_id,
                system=adapter.system,
                idempotency_key=bill_key,
                status="pending",
            )
        storage_path, filename, mime = doc.storage_path, doc.filename, doc.mime

    # Network calls happen outside any transaction; the idempotency keys make retries safe.
    try:
        result = adapter.create_bill(bill, idempotency_key=bill_key)
        attachment = adapter.attach_file(
            result.external_id,
            filename=filename or f"invoice-{bill.doc_number}",
            content_type=mime,
            data=storage.get(storage_path),
            idempotency_key=attach_key,
        )
    except AccountingError as exc:
        if not exc.retryable:
            with db.tx() as conn:
                repo.record_bill(
                    conn,
                    document_id,
                    system=adapter.system,
                    idempotency_key=bill_key,
                    status="failed",
                )
        raise

    with db.tx() as conn:
        doc = repo.get_document(conn, document_id, for_update=True)
        already = doc.status is DocStatus.POSTED  # a concurrent call finished first
        if not already:
            repo.record_bill(
                conn,
                document_id,
                system=result.system,
                idempotency_key=bill_key,
                status="posted",
                external_id=result.external_id,
                attachment_id=attachment.external_id,
            )
            repo.set_status(
                conn,
                document_id,
                DocStatus.POSTED,
                actor=actor,
                reason=f"Bill created in {result.system} ({result.status})",
                evidence=PostingReceipt(result.system, result.external_id, bill_key),
            )
    return PostResult(
        document_id,
        result.external_id,
        result.system,
        "Posted",
        already_posted=already or result.status == "already_exists",
        attachment_id=attachment.external_id,
    )
