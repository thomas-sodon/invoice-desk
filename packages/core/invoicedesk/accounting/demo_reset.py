"""Demo reset (``make demo-reset``): clear the demo board and the sandbox bills it posted.

Steps, all scoped to batches with ``mode = 'demo'`` for one tenant:

1. **Plan** (always): list demo batches, their documents and the ``bills`` rows they posted.
2. **Sandbox** (``apply`` and a :class:`QboSandboxAdmin`): delete each posted QBO bill (only
   bills carrying the InvoiceDesk marker; see :mod:`invoicedesk.accounting.sandbox_admin`).
   Optionally sweep any other marker-carrying sandbox bills (``sweep=True``) that no non-demo
   document points at. If any delete fails, the database is left untouched so a re-run can
   finish the job.
3. **Database** (``apply``): in one transaction, re-plan (refusing if a demo document posted
   to QBO since step 1), then delete the demo documents' non-audit rows
   (jobs, bills, comparison results, documents and everything that cascades from them) and the
   demo batches, then append one ``demo.reset`` audit row. ``audit_log`` is never touched.
4. **Re-seed**: call ``invoicedesk_data.seed.seed_database`` (imported lazily) so the business
   universe the board needs is present again.

Dry run is the default: nothing is changed unless ``apply=True``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, LiteralString, Protocol

from invoicedesk.audit.chain import append as audit_append
from invoicedesk.db.connection import Conn, Database
from invoicedesk.status import Actor

__all__ = [
    "DemoResetError",
    "DemoResetPlan",
    "DemoResetReport",
    "SandboxDeleter",
    "plan_demo_reset",
    "run_demo_reset",
]


class DemoResetError(RuntimeError):
    pass


class SandboxDeleter(Protocol):
    """What the reset needs from :class:`~invoicedesk.accounting.sandbox_admin.QboSandboxAdmin`."""

    def delete_bill(self, bill_id: str) -> Any: ...

    def list_invoicedesk_bills(self) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class DemoResetPlan:
    tenant_id: str
    batch_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    #: (document_id, external_id) of bills posted to QBO by demo documents
    qbo_bills: tuple[tuple[str, str], ...]
    other_bills: int  # bills rows for demo docs in other systems (fake) or without external id
    #: QBO bill ids that non-demo documents (any tenant) point at; ``sweep`` never deletes them
    protected_qbo_ids: frozenset[str] = frozenset()


@dataclass(slots=True)
class DemoResetReport:
    plan: DemoResetPlan
    applied: bool = False
    sandbox_deleted: list[str] = field(default_factory=list)
    sandbox_missing: list[str] = field(default_factory=list)
    sandbox_refused: list[str] = field(default_factory=list)
    sandbox_skipped: bool = False
    rows_deleted: dict[str, int] = field(default_factory=dict)
    seeded: Any = None

    def lines(self) -> list[str]:
        p = self.plan
        out = [
            f"tenant {p.tenant_id}: {len(p.batch_ids)} demo batch(es),"
            f" {len(p.document_ids)} document(s), {len(p.qbo_bills)} QBO sandbox bill(s),"
            f" {p.other_bills} other bill row(s)",
        ]
        if not self.applied:
            out.append("dry run: nothing changed (pass --apply to reset)")
            return out
        if self.sandbox_skipped:
            out.append("sandbox: skipped (no QBO credentials / --skip-qbo)")
        else:
            out.append(
                f"sandbox: deleted {len(self.sandbox_deleted)}, already gone"
                f" {len(self.sandbox_missing)},"
                f" refused (not InvoiceDesk) {len(self.sandbox_refused)}"
            )
        out.append(
            "database: "
            + ", ".join(f"{k} {v}" for k, v in self.rows_deleted.items())
            + " deleted; audit_log untouched"
        )
        out.append(f"re-seed: {self.seeded!r}")
        return out


def plan_demo_reset(conn: Conn, tenant_id: str) -> DemoResetPlan:
    batches = [
        str(r["id"])
        for r in conn.execute(
            "select id from batches where tenant_id = %s and mode = 'demo' order by created_at",
            (tenant_id,),
        ).fetchall()
    ]
    docs = [
        str(r["id"])
        for r in conn.execute(
            "select id from documents where batch_id = any(%s::uuid[]) order by created_at",
            (batches,),
        ).fetchall()
    ]
    bills = conn.execute(
        "select document_id, system, external_id, status from bills"
        " where document_id = any(%s::uuid[]) order by created_at",
        (docs,),
    ).fetchall()
    qbo = tuple(
        (str(b["document_id"]), str(b["external_id"]))
        for b in bills
        if b["system"] == "qbo" and b["external_id"] and b["status"] != "deleted"
    )
    protected = frozenset(
        str(r["external_id"])
        for r in conn.execute(
            "select external_id from bills where system = 'qbo' and external_id is not null"
            " and not (document_id = any(%s::uuid[]))",
            (docs,),
        ).fetchall()
    )
    return DemoResetPlan(
        tenant_id, tuple(batches), tuple(docs), qbo, len(bills) - len(qbo), protected
    )


def _default_seeder(db: Database, tenant_id: str) -> Any:
    from invoicedesk_data.seed import seed_database

    return seed_database(db, tenant_id=tenant_id)


def _clear_db(conn: Conn, plan: DemoResetPlan) -> dict[str, int]:
    docs = list(plan.document_ids)
    batches = list(plan.batch_ids)
    counts: dict[str, int] = {}
    stmts: tuple[tuple[str, LiteralString], ...] = (
        ("jobs", "delete from jobs where document_id = any(%s::uuid[])"),
        ("bills", "delete from bills where document_id = any(%s::uuid[])"),
        ("comparison_results", "delete from comparison_results where batch_id = any(%s::uuid[])"),
        ("documents", "delete from documents where id = any(%s::uuid[])"),
        ("batches", "delete from batches where id = any(%s::uuid[])"),
    )
    for table, sql in stmts:
        arg = batches if table in ("comparison_results", "batches") else docs
        counts[table] = conn.execute(sql, (arg,)).rowcount
    return counts


def run_demo_reset(
    db: Database,
    *,
    tenant_id: str = "maple_row",
    apply: bool = False,
    admin: SandboxDeleter | None = None,
    skip_qbo: bool = False,
    sweep: bool = False,
    seeder: Callable[[Database, str], Any] | None = None,
    actor: Actor | None = None,
) -> DemoResetReport:
    with db.tx() as conn:
        plan = plan_demo_reset(conn, tenant_id)
    report = DemoResetReport(plan)
    if not apply:
        return report

    # ---- sandbox first, so a failure leaves the DB pointing at what still exists
    if admin is None:
        if plan.qbo_bills and not skip_qbo:
            raise DemoResetError(
                f"{len(plan.qbo_bills)} demo bill(s) are in the QBO sandbox but no QBO credentials"
                " are configured; set them or pass --skip-qbo (bills stay in the sandbox)"
            )
        report.sandbox_skipped = True
    elif skip_qbo:
        report.sandbox_skipped = True
    else:
        targets = [ext for _, ext in plan.qbo_bills]
        if sweep:
            # untracked marker bills only: never a bill a live (non-demo) document posted
            targets += [
                b.id
                for b in admin.list_invoicedesk_bills()
                if b.id not in targets and b.id not in plan.protected_qbo_ids
            ]
        for ext in targets:
            out = admin.delete_bill(ext)
            result = getattr(out, "result", "deleted")
            {"deleted": report.sandbox_deleted, "missing": report.sandbox_missing}.get(
                result, report.sandbox_refused
            ).append(ext)

    # ---- database (one transaction, audit row inside it)
    with db.tx() as conn:
        # Re-plan inside the delete transaction: a demo document that posted to QBO after the
        # sandbox step would otherwise lose its row while its bill stays in the sandbox.
        current = plan_demo_reset(conn, tenant_id)
        posted_since = set(current.qbo_bills) - set(plan.qbo_bills)
        if posted_since and not report.sandbox_skipped:
            raise DemoResetError(
                f"{len(posted_since)} demo bill(s) were posted to QBO during the reset;"
                " nothing was deleted from the database, run demo-reset again"
            )
        plan = current
        report.plan = current
        report.rows_deleted = _clear_db(conn, plan)
        audit_append(
            conn,
            actor=(actor or Actor.system("demo_reset")).audit_label,
            action="demo.reset",
            tenant_id=tenant_id,
            after={
                "batches": list(plan.batch_ids),
                "documents": len(plan.document_ids),
                "sandbox_deleted": report.sandbox_deleted,
                "sandbox_missing": report.sandbox_missing,
                "sandbox_refused": report.sandbox_refused,
                "sandbox_skipped": report.sandbox_skipped,
                "rows_deleted": report.rows_deleted,
            },
        )
    report.applied = True
    report.seeded = (seeder or _default_seeder)(db, tenant_id)
    return report
