"""Board events (M9): status changes and the live per-document / per-batch cost meter.

:class:`PgNotifyPublisher` sends :class:`~invoicedesk.contracts.infra.BoardEvent` JSON on the
Postgres channel ``invoicedesk_events``; the API's SSE endpoint ``LISTEN``s on it
(docs/api-contract.md, Realtime). The ``documents_notify`` trigger already emits
``document.status_changed`` on every status update, so by default this publisher sends only the
other event types (the contract's split). :func:`emit_document` is what pipeline code calls
after each commit; it never lets a publishing failure break the pipeline.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg

from invoicedesk.contracts.infra import BoardEvent, EventPublisher, EventType
from invoicedesk.db.connection import Database
from invoicedesk.masking import mask_text
from invoicedesk.status import DocStatus, kanban_column

if TYPE_CHECKING:
    from invoicedesk.config.settings import Settings

__all__ = [
    "CHANNEL",
    "IN_FLIGHT",
    "PgNotifyPublisher",
    "emit_batch_completed_if_done",
    "emit_document",
]

log = logging.getLogger(__name__)

CHANNEL = "invoicedesk_events"
#: pg_notify payloads must stay under 8,000 bytes; messages are short sentences anyway.
_MAX_MESSAGE = 500

#: Statuses where the pipeline is still working on the document.
IN_FLIGHT: frozenset[str] = frozenset(
    {
        DocStatus.RECEIVED,
        DocStatus.SCREENING,
        DocStatus.EXTRACTING,
        DocStatus.EXTRACTED,
        DocStatus.VALIDATING,
    }
)


class PgNotifyPublisher:
    """``pg_notify('invoicedesk_events', <BoardEvent JSON>)`` over one autocommit connection."""

    def __init__(
        self,
        dsn: str,
        *,
        channel: str = CHANNEL,
        skip_types: Iterable[EventType] = ("document.status_changed",),
    ) -> None:
        self.dsn = dsn
        self.channel = channel
        self.skip_types = frozenset(skip_types)
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> PgNotifyPublisher:
        return cls(settings.database_url)

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def publish(self, event: BoardEvent) -> None:
        if event.type in self.skip_types:
            return
        payload = event.model_dump_json()
        with self._lock:
            try:
                self._connection().execute("select pg_notify(%s, %s)", (self.channel, payload))
            except psycopg.OperationalError:
                # One reconnect attempt (server restart, idle timeout), then give up loudly.
                self.close_locked()
                self._connection().execute("select pg_notify(%s, %s)", (self.channel, payload))

    def close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def close(self) -> None:
        with self._lock:
            self.close_locked()


def _usd(value: object) -> str:
    return BoardEvent.usd(Decimal(str(value)))


def _publish(publisher: EventPublisher, event: BoardEvent) -> None:
    try:
        publisher.publish(event)
    except Exception:  # realtime is best effort; the DB is the source of truth
        log.warning("board event publish failed (%s)", event.type, exc_info=True)


def emit_document(
    db: Database,
    publisher: EventPublisher,
    document_id: str,
    *,
    types: Iterable[EventType] = ("document.status_changed", "document.updated"),
    message: str | None = None,
) -> None:
    """Publish the document's current status and running costs (read after commit)."""
    with db.tx() as conn:
        row = conn.execute(
            "select d.tenant_id, d.batch_id, d.status, d.total_cost_usd,"
            " (select coalesce(sum(b.total_cost_usd), 0) from documents b"
            "  where d.batch_id is not null and b.batch_id = d.batch_id) as batch_cost"
            " from documents d where d.id = %s",
            (document_id,),
        ).fetchone()
    if row is None:
        return
    status = str(row["status"])
    batch_id = None if row["batch_id"] is None else str(row["batch_id"])
    msg = mask_text(message)
    common = {
        "tenant_id": row["tenant_id"],
        "document_id": document_id,
        "batch_id": batch_id,
        "status": status,
        "column": kanban_column(status),
        "cost_usd": _usd(row["total_cost_usd"]),
        "batch_cost_usd": _usd(row["batch_cost"]) if batch_id else None,
        "message": None if msg is None else msg[:_MAX_MESSAGE],
    }
    for t in types:
        _publish(publisher, BoardEvent(type=t, **common))
    if batch_id is not None:
        _publish(
            publisher,
            BoardEvent(
                type="batch.cost_updated",
                tenant_id=row["tenant_id"],
                batch_id=batch_id,
                batch_cost_usd=common["batch_cost_usd"],
            ),
        )
        emit_batch_completed_if_done(db, publisher, batch_id)


def emit_batch_completed_if_done(db: Database, publisher: EventPublisher, batch_id: str) -> bool:
    """``batch.completed`` once no document of the batch is still in flight."""
    with db.tx() as conn:
        row = conn.execute(
            "select b.tenant_id, count(d.id) as n,"
            " count(d.id) filter (where d.status::text = any(%s)) as busy,"
            " coalesce(sum(d.total_cost_usd), 0) as cost"
            " from batches b left join documents d on d.batch_id = b.id where b.id = %s"
            " group by b.tenant_id",
            (sorted(IN_FLIGHT), batch_id),
        ).fetchone()
    if row is None or row["n"] == 0 or row["busy"] > 0:
        return False
    _publish(
        publisher,
        BoardEvent(
            type="batch.completed",
            tenant_id=row["tenant_id"],
            batch_id=batch_id,
            batch_cost_usd=_usd(row["cost"]),
        ),
    )
    return True
