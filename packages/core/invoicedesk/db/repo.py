"""Repository functions. Every function takes a connection inside the caller's transaction and
writes the audit row(s) for what it changes (invariant 5). Status changes go ONLY through
:func:`set_status`, which calls :func:`invoicedesk.status.transition`."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from invoicedesk.audit.chain import append as audit_append
from invoicedesk.contracts.extractor import FieldResult, TierRun
from invoicedesk.contracts.rules import RuleResult
from invoicedesk.contracts.screening import ScreenResult
from invoicedesk.contracts.tenant_data import (
    ApprovalRecord,
    EstimateLine,
    PriorInvoice,
    Property,
    TenantData,
    Unit,
    Vendor,
    WorkOrder,
)
from invoicedesk.db.connection import Conn
from invoicedesk.masking import mask_payload
from invoicedesk.status import (
    Actor,
    ApprovalEvidence,
    DocStatus,
    DocumentFacts,
    PostingReceipt,
    RoutingDecision,
    TransitionRecord,
    transition,
)

__all__ = [
    "DocumentRow",
    "add_spend",
    "create_batch",
    "create_staff_user",
    "document_facts",
    "ensure_tenant",
    "get_document",
    "get_spend",
    "insert_document",
    "list_approvals",
    "load_tenant_data",
    "record_approval",
    "record_bill",
    "record_extraction",
    "record_review",
    "record_rule_results",
    "record_screen_results",
    "save_fields",
    "set_status",
]


class DocumentRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    tenant_id: str
    batch_id: str | None
    source: Literal["upload", "email", "seed", "mcp"]
    session_id: str | None
    uploaded_by: str | None
    filename: str | None
    sha256: str
    phash: str | None
    storage_path: str
    pages: int
    mime: str
    status: DocStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    purge_after: dt.datetime | None
    total_cost_usd: Decimal


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _doc_from_row(r: dict[str, Any]) -> DocumentRow:
    r = dict(r)
    for k in ("id", "batch_id", "uploaded_by"):
        r[k] = _s(r.get(k))
    return DocumentRow.model_validate(r)


def ensure_tenant(conn: Conn, tenant_id: str, name: str, rules_yaml: str = "") -> None:
    conn.execute(
        "insert into tenants (id, name, rules_yaml) values (%s,%s,%s) on conflict (id) do update"
        " set name = excluded.name, rules_yaml = excluded.rules_yaml",
        (tenant_id, name, rules_yaml),
    )


def create_staff_user(
    conn: Conn, *, tenant_id: str, email: str, display_name: str, role: str = "clerk"
) -> str:
    row = conn.execute(
        "insert into staff_users (tenant_id, email, display_name, role) values (%s,%s,%s,%s)"
        " on conflict (email) do update set display_name = excluded.display_name returning id",
        (tenant_id, email, display_name, role),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def create_batch(
    conn: Conn,
    *,
    tenant_id: str,
    label: str = "",
    mode: str = "live",
    session_id: str | None = None,
    created_by: str | None = None,
) -> str:
    row = conn.execute(
        "insert into batches (tenant_id, label, mode, session_id, created_by)"
        " values (%s,%s,%s,%s,%s) returning id",
        (tenant_id, label, mode, session_id, created_by),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def insert_document(
    conn: Conn,
    *,
    tenant_id: str,
    source: Literal["upload", "email", "seed", "mcp"],
    sha256: str,
    storage_path: str,
    mime: str,
    pages: int = 1,
    filename: str | None = None,
    batch_id: str | None = None,
    session_id: str | None = None,
    uploaded_by: str | None = None,
    phash: str | None = None,
    purge_after: dt.datetime | None = None,
    actor: Actor | None = None,
) -> str:
    row = conn.execute(
        "insert into documents (tenant_id, batch_id, source, session_id, uploaded_by, filename,"
        " sha256, phash, storage_path, pages, mime, purge_after)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (
            tenant_id,
            batch_id,
            source,
            session_id,
            uploaded_by,
            filename,
            sha256,
            phash,
            storage_path,
            pages,
            mime,
            purge_after,
        ),
    ).fetchone()
    assert row is not None
    doc_id = str(row["id"])
    audit_append(
        conn,
        actor=(actor or Actor.system("api")).audit_label,
        action="document.created",
        document_id=doc_id,
        tenant_id=tenant_id,
        after={"status": "Received", "source": source, "sha256": sha256, "filename": filename},
    )
    return doc_id


def get_document(conn: Conn, document_id: str, *, for_update: bool = False) -> DocumentRow:
    if for_update:
        row = conn.execute("select * from documents where id = %s for update", (document_id,))
    else:
        row = conn.execute("select * from documents where id = %s", (document_id,))
    row = row.fetchone()
    if row is None:
        raise KeyError(document_id)
    return _doc_from_row(row)


def document_facts(doc: DocumentRow) -> DocumentFacts:
    return DocumentFacts(doc.id, doc.source, doc.uploaded_by, doc.session_id)


def set_status(
    conn: Conn,
    document_id: str,
    target: DocStatus,
    *,
    actor: Actor,
    reason: str,
    evidence: RoutingDecision | ApprovalEvidence | PostingReceipt | None = None,
) -> TransitionRecord:
    """Lock the row, validate the transition, update, and write the audit row."""
    doc = get_document(conn, document_id, for_update=True)
    rec = transition(
        doc.status, target, actor=actor, facts=document_facts(doc), reason=reason, evidence=evidence
    )
    conn.execute(
        "update documents set status = %s, updated_at = now() where id = %s",
        (rec.to_status.value, document_id),
    )
    audit_append(
        conn,
        actor=actor.audit_label,
        action="status.changed",
        document_id=document_id,
        tenant_id=doc.tenant_id,
        before={"status": rec.from_status.value},
        after={"status": rec.to_status.value, "reason": reason, **rec.details},
    )
    return rec


def record_extraction(conn: Conn, document_id: str, run: TierRun) -> str:
    row = conn.execute(
        "insert into extractions (document_id, tier, model_id, output_json, refused, input_tokens,"
        " cache_read_tokens, cache_write_tokens, output_tokens, cost_usd, latency_ms, trace_id)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (
            document_id,
            run.tier,
            run.model_id,
            None
            if run.output is None
            else json.dumps(mask_payload(run.output.model_dump(mode="json"))),
            run.refused,
            run.usage.input_tokens,
            run.usage.cache_read_input_tokens,
            run.usage.cache_creation_input_tokens,
            run.usage.output_tokens,
            Decimal(run.cost_usd),
            run.latency_ms,
            run.trace_id,
        ),
    ).fetchone()
    assert row is not None
    conn.execute(
        "update documents set total_cost_usd = total_cost_usd + %s where id = %s",
        (Decimal(run.cost_usd), document_id),
    )
    return str(row["id"])


def save_fields(conn: Conn, document_id: str, fields: list[FieldResult]) -> None:
    """Replace the final field set (previous finals are kept as history, final=false)."""
    names = [f.name for f in fields]
    conn.execute(
        "update fields set final = false where document_id = %s and name = any(%s) and final",
        (document_id, names),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "insert into fields (document_id, name, value, raw_conf, calibrated_conf, word_ids,"
            " box, page, final) values (%s,%s,%s,%s,%s,%s,%s,%s,true)",
            [
                (
                    document_id,
                    f.name,
                    f.value,
                    f.raw_conf,
                    f.calibrated_conf,
                    f.word_ids,
                    list(f.box) if f.box else None,
                    f.page,
                )
                for f in fields
            ],
        )


def record_screen_results(
    conn: Conn, document_id: str, tenant_id: str, results: list[ScreenResult]
) -> None:
    for r in results:
        spans = [mask_payload(s.model_dump(mode="json")) for s in r.spans]
        conn.execute(
            "insert into screen_results (document_id, detector, verdict, categories, spans_json)"
            " values (%s,%s,%s,%s,%s)",
            (document_id, r.detector, r.verdict, list(r.categories), json.dumps(spans)),
        )
        audit_append(
            conn,
            actor=Actor.system("screening").audit_label,
            action=f"screen.{r.detector}",
            document_id=document_id,
            tenant_id=tenant_id,
            after={"verdict": r.verdict, "categories": list(r.categories), "spans": spans},
        )


def record_rule_results(
    conn: Conn, document_id: str, tenant_id: str, results: list[RuleResult]
) -> int:
    """Insert one evaluation round; returns the evaluation number."""
    row = conn.execute(
        "select coalesce(max(evaluation), 0) + 1 as n from rule_results where document_id = %s",
        (document_id,),
    ).fetchone()
    assert row is not None
    n = int(row["n"])
    for r in results:
        conn.execute(
            "insert into rule_results (document_id, rule_id, passed, severity, message,"
            " details_json, evaluation) values (%s,%s,%s,%s,%s,%s,%s)",
            (
                document_id,
                r.rule_id,
                r.passed,
                r.severity,
                r.message,
                json.dumps(mask_payload({**r.details, "fields": r.fields})),
                n,
            ),
        )
        audit_append(
            conn,
            actor=Actor.system("rules").audit_label,
            action=f"rule.{r.rule_id}",
            document_id=document_id,
            tenant_id=tenant_id,
            after={"passed": r.passed, "severity": r.severity, "message": r.message, "round": n},
        )
    return n


def record_review(
    conn: Conn,
    document_id: str,
    *,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    reviewer_id: str,
) -> str:
    doc = get_document(conn, document_id)
    row = conn.execute(
        "insert into reviews (document_id, field_name, old_value, new_value, reviewer_id)"
        " values (%s,%s,%s,%s,%s) returning id",
        (document_id, field_name, old_value, new_value, reviewer_id),
    ).fetchone()
    assert row is not None
    audit_append(
        conn,
        actor=Actor.user(reviewer_id).audit_label,
        action="review.field_corrected",
        document_id=document_id,
        tenant_id=doc.tenant_id,
        before={field_name: old_value},
        after={field_name: new_value},
    )
    return str(row["id"])


def record_approval(
    conn: Conn,
    document_id: str,
    *,
    kind: str,
    approver_id: str,
    decision: Literal["approve", "reject"],
    callback_attested: bool,
    note: str | None = None,
) -> str:
    """The DB trigger rejects self-approval by the uploader and unattested bank-change approvals."""
    doc = get_document(conn, document_id)
    row = conn.execute(
        "insert into approvals (document_id, kind, approver_id, callback_attested, decision, note)"
        " values (%s,%s,%s,%s,%s,%s) returning id",
        (document_id, kind, approver_id, callback_attested, decision, note),
    ).fetchone()
    assert row is not None
    audit_append(
        conn,
        actor=Actor.user(approver_id).audit_label,
        action=f"approval.{decision}",
        document_id=document_id,
        tenant_id=doc.tenant_id,
        after={"kind": kind, "callback_attested": callback_attested, "note": note},
    )
    return str(row["id"])


def list_approvals(conn: Conn, document_id: str) -> list[ApprovalRecord]:
    rows = conn.execute(
        "select approver_id, kind, decision, callback_attested from approvals"
        " where document_id = %s order by created_at",
        (document_id,),
    ).fetchall()
    return [
        ApprovalRecord(
            approver_id=str(r["approver_id"]),
            kind=r["kind"],
            decision=r["decision"],
            callback_attested=r["callback_attested"],
        )
        for r in rows
    ]


def record_bill(
    conn: Conn,
    document_id: str,
    *,
    system: str,
    idempotency_key: str,
    status: Literal["pending", "posted", "failed", "deleted"],
    external_id: str | None = None,
    attachment_id: str | None = None,
) -> None:
    """Upsert by idempotency key (UNIQUE), so a retried post never creates a second row."""
    doc = get_document(conn, document_id)
    conn.execute(
        "insert into bills (document_id, system, external_id, idempotency_key, status,"
        " attachment_id, posted_at) values (%s,%s,%s,%s,%s,%s, case when %s = 'posted'"
        " then now() end) on conflict (idempotency_key) do update set"
        " external_id = coalesce(excluded.external_id, bills.external_id),"
        " attachment_id = coalesce(excluded.attachment_id, bills.attachment_id),"
        " status = excluded.status, posted_at = coalesce(bills.posted_at, excluded.posted_at)",
        (document_id, system, external_id, idempotency_key, status, attachment_id, status),
    )
    audit_append(
        conn,
        actor=Actor.system("posting").audit_label,
        action=f"bill.{status}",
        document_id=document_id,
        tenant_id=doc.tenant_id,
        after={"system": system, "external_id": external_id, "idempotency_key": idempotency_key},
    )


def load_tenant_data(
    conn: Conn, tenant_id: str, *, exclude_document_id: str | None = None
) -> TenantData:
    """Load the business universe plus prior invoices (history + other processed documents)."""
    props = conn.execute(
        "select id, name, address from properties where tenant_id = %s", (tenant_id,)
    ).fetchall()
    units = conn.execute(
        "select u.id, u.property_id, u.label from units u join properties p on p.id ="
        " u.property_id where p.tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    vendors = conn.execute("select * from vendors where tenant_id = %s", (tenant_id,)).fetchall()
    wos = conn.execute("select * from work_orders where tenant_id = %s", (tenant_id,)).fetchall()
    lines = conn.execute(
        "select el.work_order_id, el.description, el.amount_cents from estimate_lines el join"
        " work_orders w on w.id = el.work_order_id where w.tenant_id = %s order by el.id",
        (tenant_id,),
    ).fetchall()
    by_wo: dict[str, list[EstimateLine]] = {}
    for ln in lines:
        by_wo.setdefault(str(ln["work_order_id"]), []).append(
            EstimateLine(description=ln["description"], amount_cents=int(ln["amount_cents"]))
        )
    hist = conn.execute(
        "select id, vendor_id, invoice_number, total_cents, invoice_date, phash from"
        " historical_bills where tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    # Previously processed documents: final fields pivoted.
    docs = conn.execute(
        "select d.id, d.sha256, d.phash, d.status,"
        " max(f.value) filter (where f.name = 'vendor_id') as vendor_id,"
        " max(f.value) filter (where f.name = 'invoice_number') as invoice_number,"
        " max(f.value) filter (where f.name = 'total') as total,"
        " max(f.value) filter (where f.name = 'invoice_date') as invoice_date"
        " from documents d left join fields f on f.document_id = d.id and f.final"
        " where d.tenant_id = %s and d.id is distinct from %s and d.session_id is null"
        " and d.status <> 'Rejected' group by d.id",
        (tenant_id, exclude_document_id),
    ).fetchall()
    prior = [
        PriorInvoice(
            source="history",
            ref_id=str(h["id"]),
            vendor_id=str(h["vendor_id"]),
            invoice_number=h["invoice_number"],
            total_cents=int(h["total_cents"]),
            invoice_date=h["invoice_date"],
            phash=h["phash"],
        )
        for h in hist
    ] + [
        PriorInvoice(
            source="document",
            ref_id=str(d["id"]),
            vendor_id=d["vendor_id"],
            invoice_number=d["invoice_number"],
            total_cents=int(d["total"]) if d["total"] not in (None, "") else None,
            invoice_date=dt.date.fromisoformat(d["invoice_date"]) if d["invoice_date"] else None,
            phash=d["phash"],
            sha256=d["sha256"],
            status=str(d["status"]),
        )
        for d in docs
    ]
    return TenantData(
        tenant_id=tenant_id,
        properties=tuple(
            Property(id=str(p["id"]), name=p["name"], address=p["address"]) for p in props
        ),
        units=tuple(
            Unit(id=str(u["id"]), property_id=str(u["property_id"]), label=u["label"])
            for u in units
        ),
        vendors=tuple(
            Vendor(
                id=str(v["id"]),
                legal_name=v["legal_name"],
                aliases=tuple(v["aliases"] or ()),
                address=v["address"],
                tax_id_last4=v["tax_id_last4"],
                remit_to_hash=v["remit_to_hash"],
                bank_last4=v["bank_last4"],
                phone_on_file=v["phone_on_file"],
                email=v["email"],
                status=v["status"],
            )
            for v in vendors
        ),
        work_orders=tuple(
            WorkOrder(
                id=str(w["id"]),
                wo_number=w["wo_number"],
                unit_id=str(w["unit_id"]),
                vendor_id=str(w["vendor_id"]),
                scope=w["scope"],
                status=w["status"],
                approved_estimate_cents=int(w["approved_estimate_cents"]),
                estimate_lines=tuple(by_wo.get(str(w["id"]), ())),
            )
            for w in wos
        ),
        prior_invoices=tuple(prior),
    )


def add_spend(
    conn: Conn,
    *,
    scope: Literal["global", "ip", "session"],
    key: str,
    usd: Decimal,
    day: dt.date | None = None,
) -> Decimal:
    """Atomically add to the spend ledger; returns the new total for (day, scope, key)."""
    day = day or dt.datetime.now(dt.UTC).date()
    row = conn.execute(
        "insert into spend_ledger (day, scope, key, usd) values (%s,%s,%s,%s) on conflict (day,"
        " scope, key) do update set usd = spend_ledger.usd + excluded.usd returning usd",
        (day, scope, key, usd),
    ).fetchone()
    assert row is not None
    return Decimal(row["usd"])


def get_spend(
    conn: Conn, *, scope: Literal["global", "ip", "session"], key: str, day: dt.date | None = None
) -> Decimal:
    day = day or dt.datetime.now(dt.UTC).date()
    row = conn.execute(
        "select usd from spend_ledger where day = %s and scope = %s and key = %s", (day, scope, key)
    ).fetchone()
    return Decimal(row["usd"]) if row else Decimal(0)
