"""Seed the Maple Row business universe (M1 demo prerequisite; DESIGN §6.2).

* :func:`write_csvs` writes ``data/seed/*.csv`` (committed, reviewable).
* :func:`seed_database` loads the universe into Postgres through ``invoicedesk.db.repo`` (tenant,
  staff users) and plain SQL with deterministic UUIDs and ``on conflict`` clauses, so re-running
  it is a no-op. It appends one ``seed.loaded`` audit row when it inserted anything.

No raw bank number is ever written: vendors carry ``remit_to_hash`` and ``bank_last4`` only.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

from invoicedesk.audit.chain import append as audit_append
from invoicedesk.db import repo
from invoicedesk.db.connection import Conn, Database

from invoicedesk_data.paths import SEED_DIR, TENANTS_DIR
from invoicedesk_data.universe import TENANT_ID, Universe, build_universe

__all__ = ["SeedCounts", "csv_tables", "seed_database", "write_csvs"]


@dataclass(frozen=True, slots=True)
class SeedCounts:
    properties: int
    units: int
    vendors: int
    work_orders: int
    estimate_lines: int
    historical_bills: int
    staff_users: int
    inserted: int  # rows newly inserted by this call (0 on a re-run)


def csv_tables(u: Universe) -> dict[str, tuple[list[str], list[list[Any]]]]:
    """``{filename: (header, rows)}`` for the committed seed CSVs."""
    vend_slug = {v.id: v.slug for v in u.vendors}
    unit_label = {x.id: x.label for x in u.units}
    prop_slug = {p.id: p.slug for p in u.properties}
    return {
        "properties.csv": (
            ["id", "slug", "name", "address"],
            [[p.id, p.slug, p.name, p.address] for p in u.properties],
        ),
        "units.csv": (
            ["id", "property_id", "property_slug", "label"],
            [[x.id, x.property_id, prop_slug[x.property_id], x.label] for x in u.units],
        ),
        "vendors.csv": (
            [
                "id",
                "slug",
                "legal_name",
                "aliases",
                "trade",
                "address",
                "tax_id_last4",
                "remit_to_hash",
                "bank_last4",
                "phone_on_file",
                "email",
                "qbo_vendor_ref",
                "status",
                "template",
            ],
            [
                [
                    v.id,
                    v.slug,
                    v.legal_name,
                    "|".join(v.aliases),
                    v.trade,
                    v.address,
                    v.tax_id_last4,
                    v.remit_to_hash,
                    v.bank_last4,
                    v.phone_on_file,
                    v.email,
                    v.qbo_vendor_ref,
                    v.status,
                    v.template,
                ]
                for v in u.vendors
            ],
        ),
        "work_orders.csv": (
            [
                "id",
                "wo_number",
                "unit_id",
                "unit_label",
                "vendor_id",
                "vendor_slug",
                "scope",
                "status",
                "approved_estimate_cents",
                "approved_by",
                "approved_at",
            ],
            [
                [
                    w.id,
                    w.wo_number,
                    w.unit_id,
                    unit_label[w.unit_id],
                    w.vendor_id,
                    vend_slug[w.vendor_id],
                    w.scope,
                    w.status,
                    w.approved_estimate_cents,
                    w.approved_by,
                    w.approved_at.isoformat(),
                ]
                for w in u.work_orders
            ],
        ),
        "estimate_lines.csv": (
            ["id", "work_order_id", "wo_number", "description", "quantity", "unit", "amount_cents"],
            [
                [e.id, w.id, w.wo_number, e.description, e.quantity, e.unit, e.amount_cents]
                for w in u.work_orders
                for e in w.estimate_lines
            ],
        ),
        "historical_bills.csv": (
            ["id", "vendor_id", "vendor_slug", "invoice_number", "invoice_date", "total_cents"],
            [
                [
                    h.id,
                    h.vendor_id,
                    vend_slug[h.vendor_id],
                    h.invoice_number,
                    h.invoice_date.isoformat(),
                    h.total_cents,
                ]
                for h in u.historical_bills
            ],
        ),
        "staff_users.csv": (
            ["email", "display_name", "role"],
            [[s.email, s.display_name, s.role] for s in u.staff],
        ),
    }


def render_csv(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def write_csvs(out_dir: Path = SEED_DIR, universe: Universe | None = None) -> list[Path]:
    u = universe or build_universe()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, (header, rows) in csv_tables(u).items():
        path = out_dir / name
        path.write_text(render_csv(header, rows), encoding="utf-8")
        written.append(path)
    return written


def _rules_yaml(tenant_id: str) -> str:
    path = TENANTS_DIR / tenant_id / "rules.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _insert(conn: Conn, sql: LiteralString, params: tuple[Any, ...]) -> int:
    cur = conn.execute(sql, params)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def seed_into(conn: Conn, universe: Universe, *, tenant_id: str = TENANT_ID) -> SeedCounts:
    """Seed inside the caller's transaction.

    Row ids are deterministic per universe tenant, so seeding the same universe under another
    tenant id would hit ``on conflict do nothing`` and silently load nothing: refuse it."""
    u = universe
    if tenant_id != u.tenant_id:
        raise ValueError(f"universe is for tenant {u.tenant_id!r}; cannot seed it as {tenant_id!r}")
    repo.ensure_tenant(conn, tenant_id, u.tenant_name, _rules_yaml(tenant_id))
    before_staff = conn.execute(
        "select count(*) as n from staff_users where tenant_id = %s", (tenant_id,)
    ).fetchone()
    for s in u.staff:
        repo.create_staff_user(
            conn, tenant_id=tenant_id, email=s.email, display_name=s.display_name, role=s.role
        )
    after_staff = conn.execute(
        "select count(*) as n from staff_users where tenant_id = %s", (tenant_id,)
    ).fetchone()
    inserted = int(after_staff["n"] if after_staff else 0) - int(
        before_staff["n"] if before_staff else 0
    )
    for p in u.properties:
        inserted += _insert(
            conn,
            "insert into properties (id, tenant_id, name, address) values (%s,%s,%s,%s)"
            " on conflict (id) do nothing",
            (p.id, tenant_id, p.name, p.address),
        )
    for x in u.units:
        inserted += _insert(
            conn,
            "insert into units (id, property_id, label) values (%s,%s,%s) on conflict do nothing",
            (x.id, x.property_id, x.label),
        )
    for v in u.vendors:
        inserted += _insert(
            conn,
            "insert into vendors (id, tenant_id, legal_name, aliases, address, tax_id_last4,"
            " remit_to_hash, bank_last4, phone_on_file, email, qbo_vendor_ref, status)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
            (
                v.id,
                tenant_id,
                v.legal_name,
                list(v.aliases),
                v.address,
                v.tax_id_last4,
                v.remit_to_hash,
                v.bank_last4,
                v.phone_on_file,
                v.email,
                v.qbo_vendor_ref,
                v.status,
            ),
        )
    for w in u.work_orders:
        inserted += _insert(
            conn,
            "insert into work_orders (id, tenant_id, wo_number, unit_id, vendor_id, scope, status,"
            " approved_estimate_cents, approved_by, approved_at)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
            (
                w.id,
                tenant_id,
                w.wo_number,
                w.unit_id,
                w.vendor_id,
                w.scope,
                w.status,
                w.approved_estimate_cents,
                w.approved_by,
                w.approved_at,
            ),
        )
        for e in w.estimate_lines:
            inserted += _insert(
                conn,
                "insert into estimate_lines (id, work_order_id, description, amount_cents)"
                " values (%s,%s,%s,%s) on conflict (id) do nothing",
                (e.id, w.id, e.description, e.amount_cents),
            )
    for h in u.historical_bills:
        inserted += _insert(
            conn,
            "insert into historical_bills (id, tenant_id, vendor_id, invoice_number,"
            " invoice_date, total_cents) values (%s,%s,%s,%s,%s,%s) on conflict (id) do nothing",
            (h.id, tenant_id, h.vendor_id, h.invoice_number, h.invoice_date, h.total_cents),
        )
    counts = SeedCounts(
        properties=len(u.properties),
        units=len(u.units),
        vendors=len(u.vendors),
        work_orders=len(u.work_orders),
        estimate_lines=sum(len(w.estimate_lines) for w in u.work_orders),
        historical_bills=len(u.historical_bills),
        staff_users=len(u.staff),
        inserted=inserted,
    )
    if inserted:
        audit_append(
            conn,
            actor="system",
            action="seed.loaded",
            document_id=None,
            tenant_id=tenant_id,
            after={
                "seed": u.seed,
                "as_of": u.as_of.isoformat(),
                "inserted": inserted,
                "properties": counts.properties,
                "units": counts.units,
                "vendors": counts.vendors,
                "work_orders": counts.work_orders,
                "historical_bills": counts.historical_bills,
            },
        )
    return counts


def seed_database(
    db: Database, tenant_id: str = TENANT_ID, *, universe: Universe | None = None
) -> SeedCounts:
    """Load the universe into ``db`` in one transaction. Idempotent."""
    u = universe or build_universe()
    with db.tx() as conn:
        return seed_into(conn, u, tenant_id=tenant_id)
