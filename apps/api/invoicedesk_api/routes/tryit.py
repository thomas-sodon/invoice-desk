"""Public try-it page (M14, US-9): Turnstile, per-IP and per-session limits, the global $/day
spend cap with a recorded-replay fallback, 24h auto-delete, never posted."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from invoicedesk.db import repo
from invoicedesk.status import Actor

from invoicedesk_api import limits
from invoicedesk_api import schemas as sc
from invoicedesk_api.access import doc_row
from invoicedesk_api.auth import (
    SESSION_COOKIE,
    RuntimeDep,
    Visitor,
    VisitorDep,
    client_ip,
    ip_key,
    session_serializer,
)
from invoicedesk_api.demo import latest_demo_batch, start_demo_run
from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime
from invoicedesk_api.settings import TRYIT_BANNER
from invoicedesk_api.uploads import check_upload, create_document, publish_created, read_limited
from invoicedesk_api.views import build_detail

router = APIRouter(prefix="/tryit")


def _remaining(rt: Runtime, v: Visitor | None) -> int:
    limit = rt.api.tryit_uploads_per_session
    if v is None:
        return limit
    with rt.db.tx() as conn:
        used = limits.count(conn, day=v.day, scope="session_upload", key=v.session_id)
    return max(0, limit - used)


def replay_batch_id(rt: Runtime) -> str:
    """The recorded replay shown when the cap is hit: the current demo batch (a replay run is
    started if none exists; it costs nothing)."""
    with rt.db.tx() as conn:
        bid = latest_demo_batch(conn, rt)
    if bid is not None:
        return bid
    return start_demo_run(
        rt, requested="replay", actor=Actor.system("tryit"), uploaded_by=None
    ).batch_id


@router.post("/session", response_model=sc.TryitSession)
def create_session(
    *,
    body: sc.TryitSessionRequest,
    request: Request,
    response: Response,
    rt: RuntimeDep,
) -> sc.TryitSession:
    ip = client_ip(request, rt)
    result = rt.turnstile.verify(body.turnstile_token, remote_ip=ip)
    if not result.success:
        raise ApiError(403, "turnstile_failed", "The human check failed. Reload and try again.")
    day = limits.today(rt)
    with rt.db.tx() as conn:
        ok = limits.take(
            conn,
            day=day,
            scope="ip_session",
            key=ip_key(rt, ip),
            limit=rt.api.tryit_sessions_per_ip_per_day,
        )
    if not ok:
        raise ApiError(429, "rate_limited", "Too many sessions from your network today.")
    sid = secrets.token_urlsafe(18)
    cookie = session_serializer(rt).dumps({"sid": sid, "day": day.isoformat()})
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=rt.api.tryit_session_ttl_s,
        httponly=True,
        secure=rt.api.cookie_secure,
        samesite="lax",
        path="/",
    )
    return sc.TryitSession(
        session_id=sid, uploads_remaining=rt.api.tryit_uploads_per_session, banner=TRYIT_BANNER
    )


@router.get("/status", response_model=sc.TryitStatus)
def status(*, rt: RuntimeDep, v: VisitorDep) -> sc.TryitStatus:
    with rt.db.tx() as conn:
        capped = limits.spend_cap_reached(conn, rt)
    return sc.TryitStatus(
        spend_cap_reached=capped,
        uploads_remaining=0 if capped else _remaining(rt, v),
        replay_batch_id=replay_batch_id(rt) if capped else None,
        banner=TRYIT_BANNER,
    )


@router.post("/upload", status_code=202, response_model=sc.TryitUploaded)
async def upload(
    *,
    request: Request,
    file: Annotated[UploadFile, File()],
    rt: RuntimeDep,
    v: VisitorDep,
) -> sc.TryitUploaded | JSONResponse:
    if v is None:
        raise ApiError(401, "unauthenticated", "Start a try-it session first.")
    with rt.db.tx() as conn:
        capped = limits.spend_cap_reached(conn, rt)
    if capped:
        bid = await run_in_threadpool(replay_batch_id, rt)
        return JSONResponse(
            status_code=200,
            content=sc.TryitReplay(replay=True, replay_batch_id=bid).model_dump(),
        )
    data = await read_limited(file)
    checked = check_upload(data, filename=file.filename, declared=file.content_type)
    ip = client_ip(request, rt)

    def _store() -> str:
        day = limits.today(rt)
        with rt.db.tx() as conn:
            if not limits.take(
                conn,
                day=v.day,
                scope="session_upload",
                key=v.session_id,
                limit=rt.api.tryit_uploads_per_session,
            ):
                raise ApiError(429, "rate_limited", "This session has used all its uploads.")
            if not limits.take(
                conn,
                day=day,
                scope="ip_upload",
                key=ip_key(rt, ip),
                limit=rt.api.tryit_uploads_per_ip_per_day,
            ):
                # raising rolls back the session counter taken above
                raise ApiError(429, "rate_limited", "Upload limit for your network reached today.")
            row = conn.execute(
                "select id from batches where tenant_id = %s and mode = 'tryit'"
                " and session_id = %s order by created_at limit 1",
                (rt.tenant, v.session_id),
            ).fetchone()
            bid = (
                str(row["id"])
                if row is not None
                else repo.create_batch(
                    conn, tenant_id=rt.tenant, label="Try it", mode="tryit", session_id=v.session_id
                )
            )
        doc_id = create_document(
            rt,
            checked,
            source="upload",
            actor=Actor.system("tryit"),
            batch_id=bid,
            session_id=v.session_id,
        )
        rt.dispatch_process(doc_id, max_tier=rt.api.tryit_max_tier)
        publish_created(rt, doc_id, bid)
        return doc_id

    doc_id = await run_in_threadpool(_store)
    return sc.TryitUploaded(document_id=doc_id)


@router.get("/documents/{document_id}", response_model=sc.DocumentDetail)
def get_document(
    *,
    document_id: str,
    rt: RuntimeDep,
    v: VisitorDep,
) -> sc.DocumentDetail:
    if v is None:
        raise ApiError(401, "unauthenticated", "Start a try-it session first.")
    with rt.db.tx() as conn:
        row = doc_row(conn, rt, document_id)
        if row["session_id"] != v.session_id:
            raise ApiError(404, "not_found", "No such document.")
        return build_detail(conn, rt, row, anonymous=True)
