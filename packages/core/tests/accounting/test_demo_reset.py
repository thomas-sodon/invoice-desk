"""Demo-only sandbox admin (bill delete) and the demo reset (DB + sandbox + re-seed)."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, LiteralString

import httpx
import pytest
from invoicedesk.accounting import QuickBooksAdapter
from invoicedesk.accounting.demo_reset import DemoResetError, plan_demo_reset, run_demo_reset
from invoicedesk.accounting.fake_server import T0, make_bill
from invoicedesk.accounting.sandbox_admin import DeleteOutcome, QboSandboxAdmin, SandboxBill
from invoicedesk.audit.chain import verify
from invoicedesk.db import Database, repo

REPO = Path(__file__).resolve().parents[4]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_script_{name}", REPO / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- sandbox admin (respx)
def test_admin_deletes_invoicedesk_bill_with_synctoken(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    bill = QuickBooksAdapter(transport, clock=lambda: T0).create_bill(
        make_bill(), idempotency_key="k"
    )
    server.bills[bill.external_id]["SyncToken"] = "3"
    admin = QboSandboxAdmin(transport)
    out = admin.delete_bill(bill.external_id)
    assert out == DeleteOutcome(bill.external_id, "deleted")
    assert bill.external_id not in server.bills
    get, delete = server.requests[-2:]
    assert get.method == "GET" and get.url.path.endswith(f"/bill/{bill.external_id}")
    assert delete.method == "POST" and delete.url.params["operation"] == "delete"
    assert delete.url.params["requestid"] == f"demo-reset-{bill.external_id}-3"
    assert delete.content == f'{{"Id":"{bill.external_id}","SyncToken":"3"}}'.encode()
    assert admin.delete_bill(bill.external_id).result == "missing"


def test_admin_refuses_bills_not_posted_by_invoicedesk(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    server.bills["9"] = {
        "Id": "9",
        "SyncToken": "0",
        "DocNumber": "HAND-1",
        "PrivateNote": "keyed by hand",
    }
    out = QboSandboxAdmin(transport).delete_bill("9")
    assert out.result == "refused" and "9" in server.bills
    with pytest.raises(ValueError):
        QboSandboxAdmin(transport).get_bill("9 or 1=1")


def test_admin_delete_race_is_missing(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    bill = QuickBooksAdapter(transport).create_bill(make_bill(), idempotency_key="k")

    def gone(request: httpx.Request) -> None:
        return None

    admin = QboSandboxAdmin(transport)
    found = admin.get_bill(bill.external_id)
    assert found is not None
    server.before.extend(
        [
            gone,
            lambda r: httpx.Response(
                400,
                json={
                    "Fault": {
                        "Error": [{"Message": "Object Not Found", "code": "610"}],
                        "type": "ValidationFault",
                    }
                },
            ),
        ]
    )
    assert admin.delete_bill(bill.external_id).result == "missing"


def test_admin_lists_marker_bills_with_paging(fake_qbo, transport, monkeypatch) -> None:
    server, _ = fake_qbo
    import invoicedesk.accounting.sandbox_admin as sa

    monkeypatch.setattr(sa, "_PAGE", 2)
    a = QuickBooksAdapter(transport)
    for i in range(3):
        a.create_bill(make_bill(doc_number=f"INV-{i}"), idempotency_key=f"k{i}")
    server.bills["1"] = {"Id": "1", "SyncToken": "0", "DocNumber": "HAND", "PrivateNote": ""}
    bills = QboSandboxAdmin(transport).list_invoicedesk_bills()
    assert sorted(b.doc_number for b in bills) == ["INV-0", "INV-1", "INV-2"]
    queries = [r.url.params["query"] for r in server.requests if r.url.path.endswith("/query")]
    assert queries == [
        "select Id, SyncToken, DocNumber, PrivateNote from Bill startposition 1 maxresults 2",
        "select Id, SyncToken, DocNumber, PrivateNote from Bill startposition 3 maxresults 2",
        "select Id, SyncToken, DocNumber, PrivateNote from Bill startposition 5 maxresults 2",
    ]


# ---------------------------------------------------------------- demo reset (Postgres)
@dataclass
class FakeAdmin:
    marker_bills: tuple[str, ...] = ()
    fail_on: str | None = None

    def __post_init__(self) -> None:
        self.deleted: list[str] = []

    def delete_bill(self, bill_id: str) -> DeleteOutcome:
        if bill_id == self.fail_on:
            raise RuntimeError("sandbox down")
        self.deleted.append(bill_id)
        return DeleteOutcome(bill_id, "missing" if bill_id == "404" else "deleted")

    def list_invoicedesk_bills(self) -> list[SandboxBill]:
        return [SandboxBill(b, "0", "X", "InvoiceDesk ref k") for b in self.marker_bills]


def _setup(db: Database) -> dict[str, Any]:
    ids: dict[str, Any] = {"demo_docs": [], "live_docs": []}
    with db.tx() as conn:
        repo.ensure_tenant(conn, "maple_row", "Maple Row Property Management")
        repo.ensure_tenant(conn, "other", "Other")
        demo = repo.create_batch(conn, tenant_id="maple_row", label="demo", mode="demo")
        live = repo.create_batch(conn, tenant_id="maple_row", label="live", mode="live")
        other_demo = repo.create_batch(conn, tenant_id="other", label="demo", mode="demo")
        for i, (batch, key) in enumerate(
            [
                (demo, "demo_docs"),
                (demo, "demo_docs"),
                (demo, "demo_docs"),
                (live, "live_docs"),
                (other_demo, "other_docs"),
            ]
        ):
            tenant = "other" if batch == other_demo else "maple_row"
            d = repo.insert_document(
                conn,
                tenant_id=tenant,
                source="seed",
                sha256=hashlib.sha256(bytes([i])).hexdigest(),
                storage_path=f"docs/{i}.pdf",
                mime="application/pdf",
                batch_id=batch,
            )
            ids.setdefault(key, []).append(d)
        d0, d1, d2 = ids["demo_docs"]
        repo.record_bill(
            conn, d0, system="qbo", idempotency_key="b0", status="posted", external_id="145"
        )
        repo.record_bill(
            conn, d1, system="qbo", idempotency_key="b1", status="posted", external_id="404"
        )
        repo.record_bill(
            conn, d2, system="fake", idempotency_key="b2", status="posted", external_id="bill-x"
        )
        repo.record_bill(
            conn,
            ids["live_docs"][0],
            system="qbo",
            idempotency_key="b3",
            status="posted",
            external_id="200",
        )
        conn.execute("insert into jobs (kind, document_id) values ('process', %s)", (d0,))
        conn.execute(
            "insert into comparison_results (batch_id, document_id, pipeline, outcome, verdict)"
            " values (%s, %s, 'naive', 'posted', 'wrong')",
            (demo, d1),
        )
        ids.update(demo=demo, live=live, other_demo=other_demo)
    return ids


def _count(db: Database, sql: LiteralString, *args: Any) -> int:
    with db.tx() as conn:
        row = conn.execute(sql, args).fetchone()
        assert row is not None
        return int(next(iter(row.values())))


@pytest.mark.postgres
def test_plan_and_dry_run_change_nothing(pg_db: Database) -> None:
    ids = _setup(pg_db)
    with pg_db.tx() as conn:
        plan = plan_demo_reset(conn, "maple_row")
    assert plan.batch_ids == (ids["demo"],)
    assert set(plan.document_ids) == set(ids["demo_docs"])
    assert sorted(ext for _, ext in plan.qbo_bills) == ["145", "404"]
    assert plan.other_bills == 1
    audit_before = _count(pg_db, "select count(*) from audit_log")
    admin = FakeAdmin()
    seeds: list[str] = []
    report = run_demo_reset(pg_db, admin=admin, seeder=lambda db, t: seeds.append(t))
    assert not report.applied and admin.deleted == [] and seeds == []
    assert _count(pg_db, "select count(*) from documents") == 5
    assert _count(pg_db, "select count(*) from audit_log") == audit_before
    assert "dry run" in "\n".join(report.lines())


@pytest.mark.postgres
def test_apply_deletes_sandbox_bills_and_demo_rows_only(pg_db: Database) -> None:
    ids = _setup(pg_db)
    audit_before = _count(pg_db, "select count(*) from audit_log")
    admin = FakeAdmin(marker_bills=("145", "900"))
    seeds: list[str] = []
    report = run_demo_reset(
        pg_db, apply=True, admin=admin, sweep=True, seeder=lambda db, t: seeds.append(t) or "ok"
    )
    assert report.applied and seeds == ["maple_row"] and report.seeded == "ok"
    assert admin.deleted == ["145", "404", "900"]
    assert report.sandbox_deleted == ["145", "900"] and report.sandbox_missing == ["404"]
    assert report.rows_deleted == {
        "jobs": 1,
        "bills": 3,
        "comparison_results": 1,
        "documents": 3,
        "batches": 1,
    }
    # live batch and the other tenant's demo batch survive
    assert _count(pg_db, "select count(*) from documents") == 2
    assert _count(pg_db, "select count(*) from batches where id = %s", ids["live"]) == 1
    assert _count(pg_db, "select count(*) from batches where id = %s", ids["other_demo"]) == 1
    assert _count(pg_db, "select count(*) from bills where external_id = '200'") == 1
    # audit rows kept (incl. the deleted documents' history) plus one demo.reset row; chain valid
    assert _count(pg_db, "select count(*) from audit_log") == audit_before + 1
    assert (
        _count(pg_db, "select count(*) from audit_log where document_id = %s", ids["demo_docs"][0])
        >= 1
    )
    with pg_db.tx() as conn:
        row = conn.execute(
            "select actor, after_json from audit_log where action = 'demo.reset'"
        ).fetchone()
        assert row is not None and row["actor"] == "system"
        assert row["after_json"]["documents"] == 3
        assert verify(conn) == (True, None)
    assert "sandbox: deleted 2" in "\n".join(report.lines())


@pytest.mark.postgres
def test_sandbox_failure_leaves_database_untouched(pg_db: Database) -> None:
    _setup(pg_db)
    with pytest.raises(RuntimeError, match="sandbox down"):
        run_demo_reset(pg_db, apply=True, admin=FakeAdmin(fail_on="404"), seeder=lambda d, t: None)
    assert _count(pg_db, "select count(*) from documents") == 5
    assert _count(pg_db, "select count(*) from audit_log where action = 'demo.reset'") == 0


@pytest.mark.postgres
def test_no_credentials_requires_skip_qbo(pg_db: Database) -> None:
    _setup(pg_db)
    with pytest.raises(DemoResetError, match="--skip-qbo"):
        run_demo_reset(pg_db, apply=True, admin=None, seeder=lambda d, t: None)
    assert _count(pg_db, "select count(*) from documents") == 5
    report = run_demo_reset(pg_db, apply=True, admin=None, skip_qbo=True, seeder=lambda d, t: None)
    assert report.sandbox_skipped and report.rows_deleted["documents"] == 3
    # skip_qbo with an admin also leaves the sandbox alone
    admin = FakeAdmin()
    run_demo_reset(pg_db, apply=True, admin=admin, skip_qbo=True, seeder=lambda d, t: None)
    assert admin.deleted == []


@pytest.mark.postgres
def test_demo_reset_script_dry_run_and_apply(pg_dsn: str, capsys) -> None:
    from invoicedesk.config.settings import Settings

    with Database(pg_dsn) as db:
        _setup(db)
    script = load_script("demo_reset")
    s = Settings(_env_file=None, DATABASE_URL=pg_dsn)  # type: ignore[call-arg]
    assert script.main([], settings=s) == 0
    assert "dry run" in capsys.readouterr().out
    assert script.main(["--apply"], settings=s) == 2  # QBO bills but offline: needs --skip-qbo
    assert "--skip-qbo" in capsys.readouterr().err
    assert script.main(["--apply", "--skip-qbo", "--no-seed"], settings=s) == 0
    out = capsys.readouterr().out
    assert "documents 3" in out and "re-seed: 'skipped'" in out


@pytest.mark.postgres
def test_default_seeder_is_lazy_data_seed(pg_db: Database) -> None:
    pytest.importorskip("invoicedesk_data.seed")
    report = run_demo_reset(pg_db, apply=True, admin=None)
    assert report.seeded is not None
    assert _count(pg_db, "select count(*) from vendors where tenant_id = 'maple_row'") > 0
