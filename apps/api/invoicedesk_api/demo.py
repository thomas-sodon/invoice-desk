"""The one-click 25-invoice demo batch (US-8, M1) and the try-it replay batch (§7.3).

A demo run clears the previous demo batch first (the same files would otherwise be flagged as
duplicates of the last run, R4), using the accounting component's ``demo-reset`` database path.
If a previous demo batch posted bills to the QBO sandbox, the API refuses (409) and asks for
``make demo-reset``, which also deletes those sandbox bills.

``llm = 'replay'`` runs the batch against the data component's gold-backed oracle: a recorded
run with no LLM cost. That is what anonymous visitors always get, what offline mode always
uses, and what the try-it page falls back to when the $3/day cap is reached.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Literal

from invoicedesk.audit.chain import append as audit_append
from invoicedesk.db.connection import Conn
from invoicedesk.status import Actor

from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime
from invoicedesk_api.uploads import check_upload, create_document, publish_created

__all__ = [
    "DemoRun",
    "delete_checkpoints",
    "ensure_replay_batch",
    "ensure_seeded",
    "latest_demo_batch",
    "start_demo_run",
]

log = logging.getLogger("invoicedesk.api.demo")
#: Reentrant: ``ensure_replay_batch`` holds it across its check and ``start_demo_run``.
_RUN_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class DemoRun:
    batch_id: str
    document_ids: list[str]
    llm: Literal["live", "replay"]


def delete_checkpoints(conn: Conn, thread_ids: list[str]) -> None:
    """Drop LangGraph checkpoint rows (they hold OCR text) for purged/reset documents."""
    if not thread_ids:
        return
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        exists = conn.execute("select to_regclass(%s) as t", (table,)).fetchone()
        if exists is not None and exists["t"] is not None:
            conn.execute(
                f"delete from {table} where thread_id = any(%s)",  # noqa: S608 - fixed names
                (thread_ids,),
            )


def ensure_seeded(rt: Runtime) -> None:
    """Load the Maple Row universe once (idempotent) so the demo routes as designed."""
    with rt.db.tx() as conn:
        row = conn.execute(
            "select count(*) as n from vendors where tenant_id = %s", (rt.tenant,)
        ).fetchone()
    if row is not None and int(row["n"]) > 0:
        return
    from invoicedesk_data.seed import seed_database

    seed_database(rt.db, rt.tenant)


def latest_demo_batch(conn: Conn, rt: Runtime) -> str | None:
    row = conn.execute(
        "select id from batches where tenant_id = %s and mode = 'demo'"
        " order by created_at desc limit 1",
        (rt.tenant,),
    ).fetchone()
    return None if row is None else str(row["id"])


def _reset_previous(rt: Runtime, actor: Actor) -> None:
    from invoicedesk.accounting.demo_reset import plan_demo_reset, run_demo_reset

    with rt.db.tx() as conn:
        plan = plan_demo_reset(conn, rt.tenant)
    if not plan.batch_ids:
        return
    if plan.qbo_bills:
        raise ApiError(
            409,
            "demo_reset_required",
            "The last demo batch posted bills to the QuickBooks sandbox. Run make demo-reset "
            "to remove them, then start the demo again.",
        )
    run_demo_reset(rt.db, tenant_id=rt.tenant, apply=True, seeder=lambda _db, _t: None, actor=actor)
    with rt.db.tx() as conn:
        delete_checkpoints(conn, list(plan.document_ids))


def start_demo_run(
    rt: Runtime,
    *,
    requested: Literal["live", "replay"],
    actor: Actor,
    uploaded_by: str | None,
) -> DemoRun:
    from invoicedesk_data.demo import demo_files

    # Offline never calls a paid API; anonymous visitors always get the recorded run.
    llm: Literal["live", "replay"] = (
        "live" if requested == "live" and not rt.offline and actor.kind == "user" else "replay"
    )
    files = demo_files()
    with _RUN_LOCK:
        ensure_seeded(rt)
        _reset_previous(rt, actor)
        with rt.db.tx() as conn:
            from invoicedesk.db import repo

            batch_id = repo.create_batch(
                conn,
                tenant_id=rt.tenant,
                label="Demo batch (recorded run)" if llm == "replay" else "Demo batch",
                mode="demo",
                created_by=uploaded_by,
            )
            conn.execute(
                "insert into demo_runs (batch_id, llm, requested_by) values (%s, %s, %s)",
                (batch_id, llm, actor.audit_label if actor.kind == "user" else "anonymous"),
            )
            audit_append(
                conn,
                actor=actor.audit_label,
                action="demo.run",
                tenant_id=rt.tenant,
                after={"batch_id": batch_id, "llm": llm, "files": len(files)},
            )
        doc_ids: list[str] = []
        for f in files:
            checked = check_upload(f.data, filename=f.filename, declared=f.mime)
            doc_ids.append(
                create_document(
                    rt,
                    checked,
                    source="seed",
                    actor=actor,
                    batch_id=batch_id,
                    uploaded_by=uploaded_by,
                )
            )
    for d in doc_ids:
        rt.dispatch_process(d, demo=True)
        publish_created(rt, d, batch_id)
    return DemoRun(batch_id=batch_id, document_ids=doc_ids, llm=llm)


def ensure_replay_batch(rt: Runtime, *, actor: Actor) -> str:
    """The current demo batch, or a new no-cost replay run when there is none.

    The check and the start happen under the run lock, so concurrent capped try-it requests
    start one run between them, not one each (each run would wipe the one before it, and the
    batch id handed to the earlier caller would 404)."""
    with _RUN_LOCK:
        with rt.db.tx() as conn:
            bid = latest_demo_batch(conn, rt)
        if bid is not None:
            return bid
        return start_demo_run(rt, requested="replay", actor=actor, uploaded_by=None).batch_id
