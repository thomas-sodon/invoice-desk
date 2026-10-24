"""In-memory job queue with the same semantics as the Postgres SKIP LOCKED queue."""

from __future__ import annotations

import threading
import time
from typing import Any

from invoicedesk.contracts.infra import Job

__all__ = ["InMemoryJobQueue"]


class InMemoryJobQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 1
        self.jobs: dict[int, dict[str, Any]] = {}

    def enqueue(
        self,
        kind: str,
        *,
        document_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> int:
        with self._lock:
            if dedupe_key is not None:
                for jid, j in self.jobs.items():
                    if j["dedupe_key"] == dedupe_key and j["status"] in ("queued", "running"):
                        return jid
            jid = self._next
            self._next += 1
            self.jobs[jid] = {
                "kind": kind,
                "document_id": document_id,
                "payload": payload or {},
                "dedupe_key": dedupe_key,
                "status": "queued",
                "attempts": 0,
                "max_attempts": max_attempts,
                "run_after": 0.0,
                "last_error": None,
            }
            return jid

    def claim(self, worker_id: str, *, kinds: list[str] | None = None) -> Job | None:
        now = time.monotonic()
        with self._lock:
            for jid, j in sorted(self.jobs.items()):
                if j["status"] != "queued" or j["run_after"] > now:
                    continue
                if kinds is not None and j["kind"] not in kinds:
                    continue
                j["status"] = "running"
                j["attempts"] += 1
                j["locked_by"] = worker_id
                return Job(
                    id=jid,
                    kind=j["kind"],
                    document_id=j["document_id"],
                    payload=j["payload"],
                    attempts=j["attempts"],
                    max_attempts=j["max_attempts"],
                )
        return None

    def complete(self, job_id: int) -> None:
        with self._lock:
            self.jobs[job_id]["status"] = "done"

    def fail(self, job_id: int, error: str, *, retry_in_s: float | None = None) -> None:
        with self._lock:
            j = self.jobs[job_id]
            j["last_error"] = error
            if j["attempts"] >= j["max_attempts"]:
                j["status"] = "failed"
            else:
                j["status"] = "queued"
                delay = retry_in_s if retry_in_s is not None else 2 ** j["attempts"]
                j["run_after"] = time.monotonic() + delay
