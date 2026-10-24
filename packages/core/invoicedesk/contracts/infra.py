"""Contracts for infrastructure ports: object storage, realtime events, job queue, spend."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BoardEvent",
    "EventPublisher",
    "Job",
    "JobQueue",
    "Storage",
    "StoredObject",
]


class StoredObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    size: int
    content_type: str
    sha256: str


@runtime_checkable
class Storage(Protocol):
    """Supabase Storage in production; local filesystem / in-memory offline.

    Key layout: ``<tenant>/originals/<document_id>/<filename>``,
    ``<tenant>/renders/<document_id>/page-<n>.png``, ``visitor/<session_id>/...`` (24h purge).
    """

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def signed_url(self, key: str, *, expires_in_s: int = 3600) -> str: ...


EventType = Literal[
    "document.created",
    "document.status_changed",
    "document.updated",  # fields/rules/cost changed, re-fetch
    "batch.cost_updated",
    "batch.completed",
    "followup.drafted",
]


class BoardEvent(BaseModel):
    """Realtime event shape (docs/api-contract.md §Realtime)."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    tenant_id: str
    document_id: str | None = None
    batch_id: str | None = None
    status: str | None = None
    column: str | None = None
    #: USD as decimal string, e.g. "0.0112" (never a float)
    cost_usd: str | None = None
    batch_cost_usd: str | None = None
    message: str | None = None
    ts: str = Field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())

    @staticmethod
    def usd(value: Decimal) -> str:
        return format(value.normalize(), "f")


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: BoardEvent) -> None: ...


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    kind: str  # "process_document" | "resume_document" | "post_bill" | "send_followup" | ...
    document_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


@runtime_checkable
class JobQueue(Protocol):
    """Postgres ``SKIP LOCKED`` queue in production (invoicedesk.db.queue)."""

    def enqueue(
        self,
        kind: str,
        *,
        document_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> int: ...
    def claim(self, worker_id: str, *, kinds: list[str] | None = None) -> Job | None: ...
    def complete(self, job_id: int) -> None: ...
    def fail(self, job_id: int, error: str, *, retry_in_s: float | None = None) -> None: ...
