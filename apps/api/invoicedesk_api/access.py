"""Who may read which document or batch.

* Staff of the tenant read every document of the tenant.
* Anonymous callers (US-8: the 25-invoice demo runs with no signup) read only documents of
  demo batches, through the anonymous view (no bank details, original, trace or approvals).
* Try-it visitors read only their own session's documents (``/tryit/documents/{id}``).
"""

from __future__ import annotations

import uuid
from typing import Any

from invoicedesk.db.connection import Conn

from invoicedesk_api.auth import Staff
from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime

__all__ = ["batch_row", "doc_row", "is_public_batch", "parse_uuid", "readable_doc"]


def parse_uuid(value: str, what: str = "document") -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError(404, "not_found", f"No such {what}.") from exc


def doc_row(conn: Conn, rt: Runtime, document_id: str, *, lock: bool = False) -> dict[str, Any]:
    doc_id = parse_uuid(document_id)
    query = (
        "select d.*, b.mode as batch_mode from documents d left join batches b on b.id = d.batch_id"
        " where d.id = %s and d.tenant_id = %s for update of d"
        if lock
        else "select d.*, b.mode as batch_mode from documents d left join batches b"
        " on b.id = d.batch_id where d.id = %s and d.tenant_id = %s"
    )
    row = conn.execute(query, (doc_id, rt.tenant)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such document.")
    return dict(row)


def batch_row(conn: Conn, rt: Runtime, batch_id: str) -> dict[str, Any]:
    bid = parse_uuid(batch_id, "batch")
    row = conn.execute(
        "select * from batches where id = %s and tenant_id = %s", (bid, rt.tenant)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "No such batch.")
    return dict(row)


def is_public_batch(batch: dict[str, Any]) -> bool:
    return batch.get("mode") == "demo"


def readable_doc(
    conn: Conn, rt: Runtime, document_id: str, staff: Staff | None
) -> tuple[dict[str, Any], bool]:
    """``(row, anonymous_view)``; 401 for anonymous callers outside the public demo."""
    row = doc_row(conn, rt, document_id)
    if staff is not None:
        return row, False
    if row.get("batch_mode") == "demo" and row["session_id"] is None:
        return row, True
    raise ApiError(401, "unauthenticated", "Sign in to see this invoice.")
