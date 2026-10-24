"""24h auto-delete of try-it visitor uploads (M14; CLAUDE.md, Data and safety).

Deletes every visitor document whose ``purge_after`` has passed: its storage objects (original
and page renders under ``visitor/<session>/``), its LangGraph checkpoints (they hold OCR text),
the jobs, and the ``documents`` row (fields, extractions, screen and rule results, pipeline
steps cascade). ``audit_log`` rows stay: they have no foreign key, and one ``document.purged``
row per document records the deletion. Visitor documents are never posted, so no bill can
block the delete; if one ever exists the document is skipped and reported.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from invoicedesk.audit.chain import append as audit_append
from invoicedesk.contracts.infra import Storage
from invoicedesk.db.connection import Database
from invoicedesk.status import Actor

__all__ = ["PurgeReport", "purge_expired"]

log = logging.getLogger("invoicedesk.api.purge")


@dataclass
class PurgeReport:
    documents: list[str] = field(default_factory=list)
    objects_deleted: int = 0
    skipped: list[str] = field(default_factory=list)
    batches_deleted: int = 0


def _objects_for(storage: Storage, session_id: str, document_id: str, original: str) -> list[str]:
    keys = {original} if original and original != "pending" else set()
    try:
        keys.update(k for k in storage.list(f"visitor/{session_id}/") if f"/{document_id}/" in k)
    except (OSError, ValueError):
        log.warning("could not list visitor objects for a purge")
    return sorted(keys)


def purge_expired(
    db: Database, storage: Storage, *, now: dt.datetime | None = None, limit: int = 500
) -> PurgeReport:
    from invoicedesk_api.demo import delete_checkpoints

    when = now or dt.datetime.now(dt.UTC)
    report = PurgeReport()
    actor = Actor.system("purge").audit_label
    with db.tx() as conn:
        rows = conn.execute(
            "select id, tenant_id, session_id, storage_path, sha256 from documents"
            " where session_id is not null and purge_after < %s order by purge_after limit %s",
            (when, limit),
        ).fetchall()
    for r in rows:
        doc_id = str(r["id"])
        for key in _objects_for(storage, r["session_id"], doc_id, r["storage_path"]):
            try:
                storage.delete(key)
                report.objects_deleted += 1
            except (OSError, ValueError, KeyError):
                log.warning("could not delete a visitor object")
        with db.tx() as conn:
            billed = conn.execute(
                "select 1 from bills where document_id = %s limit 1", (doc_id,)
            ).fetchone()
            if billed is not None:  # unreachable by design (visitor docs are never posted)
                report.skipped.append(doc_id)
                continue
            conn.execute("delete from jobs where document_id = %s", (doc_id,))
            delete_checkpoints(conn, [doc_id])
            conn.execute("delete from documents where id = %s", (doc_id,))
            audit_append(
                conn,
                actor=actor,
                action="document.purged",
                document_id=doc_id,
                tenant_id=r["tenant_id"],
                after={"reason": "try-it upload older than 24h", "sha256": r["sha256"]},
            )
        report.documents.append(doc_id)
    with db.tx() as conn:
        report.batches_deleted = conn.execute(
            "delete from batches b where b.mode = 'tryit' and not exists"
            " (select 1 from documents d where d.batch_id = b.id)"
            " and b.created_at < %s",
            (when - dt.timedelta(hours=24),),
        ).rowcount
    return report
