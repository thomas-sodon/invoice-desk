"""Realtime board feed: ``GET /api/v1/events?batch_id=`` as Server-Sent Events.

Backed by Postgres ``LISTEN invoicedesk_events``: the ``documents_notify`` trigger emits
``document.status_changed`` and the pipeline ``pg_notify``\\ s the other event types with the
same payload. Every message is normalized to the full :class:`BoardEvent` shape (the trigger
sends a subset), filtered to the caller's tenant (and batch), and written as
``data: <json>``; a ``: keepalive`` comment goes out every 15 s.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.masking import mask_text
from invoicedesk.pipeline.events import CHANNEL
from invoicedesk.status import kanban_column
from pydantic import ValidationError

from invoicedesk_api.access import batch_row, is_public_batch
from invoicedesk_api.auth import OptionalStaffDep, RuntimeDep
from invoicedesk_api.errors import ApiError

router = APIRouter()
log = logging.getLogger("invoicedesk.api.events")


def normalize_event(raw: str) -> BoardEvent | None:
    """Parse a NOTIFY payload into a full BoardEvent (None when it is not one)."""
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    allowed = set(BoardEvent.model_fields)
    clean = {k: v for k, v in data.items() if k in allowed}
    if clean.get("status") and not clean.get("column"):
        try:
            clean["column"] = kanban_column(str(clean["status"]))
        except ValueError:
            clean["column"] = None
    for k in ("document_id", "batch_id"):
        if clean.get(k) is not None:
            clean[k] = str(clean[k])
    if clean.get("message") is not None:
        clean["message"] = mask_text(str(clean["message"]))
    clean.setdefault("ts", dt.datetime.now(dt.UTC).isoformat())
    try:
        return BoardEvent.model_validate(clean)
    except ValidationError:
        return None


def _sse(event: BoardEvent) -> str:
    return f"data: {json.dumps(event.model_dump(mode='json'), separators=(',', ':'))}\n\n"


async def event_stream(
    dsn: str,
    *,
    tenant_id: str,
    batch_id: str | None,
    keepalive_s: float,
    max_events: int | None = None,
    is_disconnected: Any = None,
) -> AsyncIterator[str]:
    """Yield SSE frames from ``LISTEN invoicedesk_events`` until the client goes away."""
    sent = 0
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"LISTEN {CHANNEL}")
        yield ": connected\n\n"
        while True:
            got_any = False
            async for note in conn.notifies(timeout=keepalive_s):
                ev = normalize_event(note.payload)
                if ev is None or ev.tenant_id != tenant_id:
                    continue
                if batch_id is not None and ev.batch_id != batch_id:
                    continue
                yield _sse(ev)
                got_any = True
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
            if is_disconnected is not None and await is_disconnected():
                return
            if not got_any:
                yield ": keepalive\n\n"


@router.get("/events")
async def events(
    *,
    request: Request,
    batch_id: str | None = None,
    rt: RuntimeDep,
    staff: OptionalStaffDep,
) -> StreamingResponse:
    bid: str | None = None
    if batch_id:
        with rt.db.tx() as conn:
            batch = batch_row(conn, rt, batch_id)
        if staff is None and not is_public_batch(batch):
            raise ApiError(401, "unauthenticated", "Sign in to follow this batch.")
        bid = str(batch["id"])
    elif staff is None:
        raise ApiError(401, "unauthenticated", "Sign in to follow the board.")
    stream = event_stream(
        rt.db.dsn,
        tenant_id=rt.tenant,
        batch_id=bid,
        keepalive_s=rt.api.sse_keepalive_s,
        max_events=rt.sse_max_events,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
