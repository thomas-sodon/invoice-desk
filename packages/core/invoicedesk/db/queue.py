"""Postgres ``FOR UPDATE SKIP LOCKED`` job queue (DESIGN §4.1). Implements contracts.JobQueue."""

from __future__ import annotations

import json
from typing import Any

from invoicedesk.contracts.infra import Job
from invoicedesk.db.connection import Database

__all__ = ["PostgresJobQueue"]


class PostgresJobQueue:
    def __init__(self, db: Database, *, base_backoff_s: float = 2.0) -> None:
        self.db = db
        self.base_backoff_s = base_backoff_s

    def enqueue(
        self,
        kind: str,
        *,
        document_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> int:
        with self.db.tx() as conn:
            row = conn.execute(
                "insert into jobs (kind, document_id, payload, dedupe_key, max_attempts)"
                " values (%s,%s,%s,%s,%s) on conflict (dedupe_key) where dedupe_key is not null"
                " and status in ('queued','running') do nothing returning id",
                (kind, document_id, json.dumps(payload or {}), dedupe_key, max_attempts),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "select id from jobs where dedupe_key = %s and status in ('queued','running')",
                    (dedupe_key,),
                ).fetchone()
            assert row is not None
            return int(row["id"])

    def claim(self, worker_id: str, *, kinds: list[str] | None = None) -> Job | None:
        with self.db.tx() as conn:
            row = conn.execute(
                "update jobs set status = 'running', attempts = attempts + 1, locked_by = %s,"
                " locked_at = now(), updated_at = now() where id = (select id from jobs where"
                " status = 'queued' and run_after <= now() and (%s::text[] is null or kind ="
                " any(%s::text[])) order by run_after, id for update skip locked limit 1)"
                " returning id, kind, document_id, payload, attempts, max_attempts",
                (worker_id, kinds, kinds),
            ).fetchone()
            if row is None:
                return None
            return Job(
                id=int(row["id"]),
                kind=row["kind"],
                document_id=None if row["document_id"] is None else str(row["document_id"]),
                payload=row["payload"],
                attempts=int(row["attempts"]),
                max_attempts=int(row["max_attempts"]),
            )

    def complete(self, job_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "update jobs set status = 'done', updated_at = now() where id = %s", (job_id,)
            )

    def fail(self, job_id: int, error: str, *, retry_in_s: float | None = None) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "update jobs set last_error = %s, updated_at = now(), locked_by = null,"
                " status = case when attempts >= max_attempts then 'failed'::job_status"
                " else 'queued'::job_status end,"
                " run_after = now()"
                " + make_interval(secs => coalesce(%s, %s * power(2, attempts - 1)))"
                " where id = %s",
                (error[:4000], retry_in_s, self.base_backoff_s, job_id),
            )

    def requeue_stale(self, *, older_than_s: int = 300) -> int:
        """Return jobs whose worker died (locked too long) to the queue."""
        with self.db.tx() as conn:
            cur = conn.execute(
                "update jobs set status = 'queued', locked_by = null, updated_at = now()"
                " where status = 'running' and locked_at < now() - make_interval(secs => %s)",
                (older_than_s,),
            )
            return cur.rowcount
