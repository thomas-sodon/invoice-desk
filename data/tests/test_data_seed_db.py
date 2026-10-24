"""Seeding into a throwaway Postgres (pg_db fixture)."""

from __future__ import annotations

import pytest
from invoicedesk.audit.chain import verify
from invoicedesk.db import repo
from invoicedesk.db.connection import Database
from invoicedesk_data.eval_examples import load_eval_examples
from invoicedesk_data.seed import seed_database
from invoicedesk_data.universe import build_universe
from psycopg import sql

pytestmark = pytest.mark.postgres


def _count(db: Database, table: str) -> int:
    with db.tx() as conn:
        query = sql.SQL("select count(*) as n from {}").format(sql.Identifier(table))
        row = conn.execute(query).fetchone()
        assert row is not None
        return int(row["n"])


def test_seed_loads_expected_counts_and_is_idempotent(pg_db: Database) -> None:
    c = seed_database(pg_db)
    assert c.inserted > 0
    assert _count(pg_db, "properties") == 6
    assert _count(pg_db, "units") == 40
    assert _count(pg_db, "vendors") == 30
    assert _count(pg_db, "work_orders") == 160
    assert _count(pg_db, "estimate_lines") == c.estimate_lines
    assert _count(pg_db, "historical_bills") == c.historical_bills
    assert _count(pg_db, "staff_users") == 3
    with pg_db.tx() as conn:
        rows = conn.execute("select status, count(*) as n from vendors group by status").fetchall()
        assert {r["status"]: r["n"] for r in rows} == {"approved": 24, "pending": 4, "blocked": 2}
        audit = conn.execute(
            "select count(*) as n from audit_log where action = 'seed.loaded'"
        ).fetchone()
        assert audit is not None and audit["n"] == 1
    again = seed_database(pg_db)
    assert again.inserted == 0
    assert _count(pg_db, "vendors") == 30
    with pg_db.tx() as conn:
        audit = conn.execute(
            "select count(*) as n from audit_log where action = 'seed.loaded'"
        ).fetchone()
        assert audit is not None and audit["n"] == 1
        assert verify(conn) == (True, None)


def test_loaded_tenant_data_matches_the_universe(pg_db: Database) -> None:
    seed_database(pg_db)
    u = build_universe()
    expected = u.to_tenant_data()
    with pg_db.tx() as conn:
        td = repo.load_tenant_data(conn, "maple_row")
    assert {v.id for v in td.vendors} == {v.id for v in expected.vendors}
    wo = td.work_order("WO-1187")
    assert wo is not None
    assert wo.approved_estimate_cents == 320_000
    assert sum(e.amount_cents for e in wo.estimate_lines) == 320_000
    unit = td.unit(wo.unit_id)
    assert unit is not None and unit.label == "12B"
    rapid = next(v for v in td.vendors if v.legal_name == "Rapid Rooter Plumbing LLC")
    assert rapid == next(v for v in expected.vendors if v.id == rapid.id)
    hist = [p for p in td.prior_invoices if p.invoice_number == "INV-2231"]
    assert len(hist) == 1 and hist[0].total_cents == 124_000
    assert len(td.prior_invoices) == len(u.historical_bills)


def test_eval_examples_load_idempotently(pg_db: Database) -> None:
    n = load_eval_examples(pg_db)
    assert n == 120 + 38 + 26
    assert load_eval_examples(pg_db) == n
    with pg_db.tx() as conn:
        rows = conn.execute(
            "select split, count(*) as n from eval_examples group by split"
        ).fetchall()
    assert {r["split"]: r["n"] for r in rows} == {
        "dev": 60,
        "test": 60,
        "adversarial": 38,
        "demo": 26,
    }
