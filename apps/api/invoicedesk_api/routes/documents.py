"""Board and document reads: ``GET /documents``, ``GET /documents/{id}``, ``GET /batches/{id}``,
``GET /documents/{id}/audit``, ``GET /files/{key}`` and ``GET /me``."""

from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, LiteralString

from fastapi import APIRouter, Query
from fastapi.responses import Response
from invoicedesk.audit.chain import verify as audit_verify
from invoicedesk.status import KANBAN_COLUMNS, DocStatus
from invoicedesk.storage import LocalStorage, safe_key

from invoicedesk_api import schemas as sc
from invoicedesk_api.access import batch_row, is_public_batch, parse_uuid, readable_doc
from invoicedesk_api.auth import OptionalStaffDep, RuntimeDep, StaffDep
from invoicedesk_api.errors import ApiError
from invoicedesk_api.views import build_cards, build_detail, iso, usd

router = APIRouter()
files_router = APIRouter()


def _encode_cursor(created_at: dt.datetime, doc_id: str) -> str:
    raw = f"{created_at.isoformat()}|{doc_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, str]:
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, _, doc_id = base64.urlsafe_b64decode(cursor + pad).decode().partition("|")
        return dt.datetime.fromisoformat(ts), parse_uuid(doc_id)
    except (ValueError, UnicodeDecodeError, ApiError) as exc:
        raise ApiError(422, "validation_error", "cursor: invalid cursor") from exc


def _statuses_for_column(column: str) -> list[str]:
    statuses = [s.value for s, c in KANBAN_COLUMNS.items() if c == column]
    if not statuses:
        raise ApiError(422, "validation_error", f"column: unknown column {column!r}")
    return statuses


@router.get("/documents", response_model=sc.DocumentList)
def list_documents(
    *,
    batch_id: str | None = None,
    column: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> sc.DocumentList:
    where: list[LiteralString] = ["d.tenant_id = %s", "d.session_id is null"]
    params: list[Any] = [rt.tenant]
    with rt.db.tx() as conn:
        if batch_id:
            batch = batch_row(conn, rt, batch_id)
            if staff is None and not is_public_batch(batch):
                raise ApiError(401, "unauthenticated", "Sign in to see this batch.")
            where.append("d.batch_id = %s")
            params.append(batch["id"])
        elif staff is None:
            raise ApiError(401, "unauthenticated", "Sign in to see the board.")
        if column:
            where.append("d.status::text = any(%s)")
            params.append(_statuses_for_column(column))
        if status:
            try:
                where.append("d.status = %s")
                params.append(DocStatus(status).value)
            except ValueError as exc:
                raise ApiError(422, "validation_error", f"status: unknown {status!r}") from exc
        if cursor:
            ts, last = _decode_cursor(cursor)
            where.append("(d.created_at, d.id) > (%s, %s)")
            params.extend([ts, last])
        # Every clause is a constant from this function; values are bound parameters.
        query: LiteralString = (
            "select d.* from documents d where "  # noqa: S608
            + " and ".join(where)
            + " order by d.created_at, d.id limit %s"
        )
        rows = conn.execute(query, (*params, limit + 1)).fetchall()
        docs = [dict(r) for r in rows[:limit]]
        cards = build_cards(conn, rt, docs)
    nxt = (
        _encode_cursor(docs[-1]["created_at"], str(docs[-1]["id"]))
        if len(rows) > limit and docs
        else None
    )
    return sc.DocumentList(items=cards, next_cursor=nxt)


@router.get("/batches/{batch_id}", response_model=sc.BatchView)
def get_batch(
    *,
    batch_id: str,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> sc.BatchView:
    with rt.db.tx() as conn:
        batch = batch_row(conn, rt, batch_id)
        if staff is None and not is_public_batch(batch):
            raise ApiError(401, "unauthenticated", "Sign in to see this batch.")
        docs = [
            dict(r)
            for r in conn.execute(
                "select * from documents where batch_id = %s order by created_at, id",
                (batch["id"],),
            ).fetchall()
        ]
        cards = build_cards(conn, rt, docs)
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.column] = counts.get(c.column, 0) + 1
    total = sum((Decimal(d["total_cost_usd"]) for d in docs), Decimal(0))
    return sc.BatchView(
        batch=sc.BatchInfo(
            id=str(batch["id"]),
            label=batch["label"],
            mode=batch["mode"],
            created_at=iso(batch["created_at"]) or "",
        ),
        counts=counts,
        cost_usd=usd(total),
        documents=cards,
    )


@router.get("/documents/{document_id}", response_model=sc.DocumentDetail)
def get_document(
    *,
    document_id: str,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> sc.DocumentDetail:
    with rt.db.tx() as conn:
        row, anonymous = readable_doc(conn, rt, document_id, staff)
        return build_detail(conn, rt, row, anonymous=anonymous)


@router.get("/documents/{document_id}/audit", response_model=sc.AuditView)
def get_audit(
    *,
    document_id: str,
    rt: RuntimeDep,
    staff: StaffDep,
) -> sc.AuditView:
    with rt.db.tx() as conn:
        row, _ = readable_doc(conn, rt, document_id, staff)
        entries = [
            sc.AuditEntry(
                id=int(r["id"]),
                ts=iso(r["ts"]) or "",
                actor=r["actor"],
                action=r["action"],
                before=r["before_json"],
                after=r["after_json"],
                row_hash=r["row_hash"],
            )
            for r in conn.execute(
                "select id, ts, actor, action, before_json, after_json, row_hash from audit_log"
                " where document_id = %s order by id",
                (row["id"],),
            ).fetchall()
        ]
        ok, _bad = audit_verify(conn)
    return sc.AuditView(entries=entries, chain_ok=ok)


@router.get("/me", response_model=sc.Me)
def me(staff: StaffDep) -> sc.Me:
    return sc.Me(id=staff.id, email=staff.email, display_name=staff.display_name, role=staff.role)


@files_router.get("/files/{key:path}")
def get_file(
    *,
    key: str,
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=128)],
    rt: RuntimeDep,
) -> Response:
    """Serve a LocalStorage object behind its HMAC-signed, expiring URL (no other auth)."""
    storage = rt.storage
    if not isinstance(storage, LocalStorage):
        raise ApiError(404, "not_found", "Files are served by the storage provider.")
    try:
        k = safe_key(key)
    except ValueError as exc:
        raise ApiError(404, "not_found", "No such file.") from exc
    if not storage.verify(k, exp, sig):
        raise ApiError(403, "forbidden", "This link is invalid or has expired.")
    if not storage.exists(k):
        raise ApiError(404, "not_found", "No such file.")
    return Response(
        content=storage.get(k),
        media_type=storage.content_type(k),
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
