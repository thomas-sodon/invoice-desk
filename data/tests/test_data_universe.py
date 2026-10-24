"""Business universe: counts, demo entities, determinism, fictional-only data."""

from __future__ import annotations

import re

from invoicedesk.config.tenant import load_rules_config
from invoicedesk.remit import remit_fingerprint
from invoicedesk.rules.vendor_match import resolve_vendor
from invoicedesk.testing.builders import make_extraction, text
from invoicedesk_data.generator.sampler import NEW_VENDOR_NAMES
from invoicedesk_data.paths import SEED_DIR, TENANTS_DIR
from invoicedesk_data.seed import csv_tables, render_csv
from invoicedesk_data.universe import (
    LOOKALIKE_VENDOR,
    TEST_ROUTING_NUMBER,
    build_universe,
    remit_text,
)


def test_counts_match_spec() -> None:
    u = build_universe()
    assert len(u.properties) == 6
    assert len(u.units) == 40
    assert len(u.vendors) == 30
    statuses = [v.status for v in u.vendors]
    assert statuses.count("approved") == 24
    assert statuses.count("pending") == 4
    assert statuses.count("blocked") == 2
    assert len(u.work_orders) == 160
    assert all(w.estimate_lines for w in u.work_orders)
    assert all(
        w.approved_estimate_cents == sum(e.amount_cents for e in w.estimate_lines)
        for w in u.work_orders
    )
    assert len(u.staff) == 3
    assert {s.role for s in u.staff} == {"clerk", "approver", "owner"}


def test_history_spans_18_months() -> None:
    u = build_universe()
    dates = sorted(h.invoice_date for h in u.historical_bills)
    assert (dates[-1] - dates[0]).days >= 540
    assert all(isinstance(h.total_cents, int) for h in u.historical_bills)
    pending = {v.id for v in u.vendors if v.status == "pending"}
    assert not any(h.vendor_id in pending for h in u.historical_bills)


def test_demo_entities() -> None:
    u = build_universe()
    rapid = u.vendor_by_slug("rapid-rooter")
    assert rapid.legal_name == "Rapid Rooter Plumbing LLC"
    assert rapid.status == "approved"
    wo = u.work_order("WO-1187")
    assert wo.vendor_id == rapid.id
    assert u.unit_by_id(wo.unit_id).label == "12B"
    assert wo.status == "open"
    # $3,200 so that "$340 over" is outside max(10 %, $100) (see docs/GAPS.md, data section)
    assert wo.approved_estimate_cents == 320_000
    inv2231 = [h for h in u.historical_bills if h.invoice_number == "INV-2231"]
    assert len(inv2231) == 1
    assert inv2231[0].vendor_id == rapid.id
    assert inv2231[0].total_cents == 124_000
    assert inv2231[0].invoice_date.isoformat() == "2026-10-10"
    assert all(v.legal_name != LOOKALIKE_VENDOR for v in u.vendors)


def test_lookalike_and_builder_extraction_resolve_as_intended() -> None:
    u = build_universe()
    td = u.to_tenant_data()
    cfg = load_rules_config("maple_row", TENANTS_DIR)
    # the shared test builder's invoice resolves to Rapid Rooter exactly
    m = resolve_vendor(make_extraction(), td, cfg)
    assert m.method == "exact"
    assert m.vendor is not None and m.vendor.legal_name == "Rapid Rooter Plumbing LLC"
    m = resolve_vendor(make_extraction(vendor_name=text(LOOKALIKE_VENDOR)), td, cfg)
    assert m.lookalike
    assert m.resembles is not None and m.resembles.legal_name == "Rapid Rooter Plumbing LLC"


def test_every_vendor_resolves_exactly_and_no_vendor_is_a_lookalike_of_another() -> None:
    u = build_universe()
    td = u.to_tenant_data()
    cfg = load_rules_config("maple_row", TENANTS_DIR)
    for v in u.vendors:
        for name in (v.legal_name, *v.aliases):
            m = resolve_vendor(make_extraction(vendor_name=text(name)), td, cfg)
            assert m.method == "exact" and m.vendor is not None and m.vendor.id == v.id, name
    for name, address in NEW_VENDOR_NAMES:
        ex = make_extraction(
            vendor_name=text(name), vendor_address=text(address), vendor_tax_id_last4=text("0000")
        )
        m = resolve_vendor(ex, td, cfg)
        assert m.method == "unknown", (name, m.method)


def test_deterministic() -> None:
    assert build_universe() == build_universe()
    assert build_universe(seed=1) != build_universe()


def test_committed_csvs_match_generator() -> None:
    for name, (header, rows) in csv_tables(build_universe()).items():
        committed = (SEED_DIR / name).read_text(encoding="utf-8")
        assert committed == render_csv(header, rows), f"data/seed/{name} is stale"


def test_vendor_master_stores_no_raw_bank_numbers() -> None:
    u = build_universe()
    vendors_csv = (SEED_DIR / "vendors.csv").read_text(encoding="utf-8")
    for v in u.vendors:
        full = remit_text(v.slug, v.legal_name)
        assert remit_fingerprint(full) == v.remit_to_hash
        account = full.rsplit("Acct ", 1)[1]
        assert account not in vendors_csv
        assert account.endswith(v.bank_last4)
        assert re.fullmatch(r"[0-9a-f]{64}", v.remit_to_hash)
    assert TEST_ROUTING_NUMBER not in vendors_csv


def test_reserved_formats_only() -> None:
    u = build_universe()
    for v in u.vendors:
        assert re.fullmatch(r"\(541\) 555-01\d\d", v.phone_on_file), v.phone_on_file
        assert v.email.endswith(".example")
        assert re.fullmatch(r"\d{4}", v.tax_id_last4)
        assert remit_text(v.slug, v.legal_name).count(TEST_ROUTING_NUMBER) == 1
        assert "Acct 000999" in remit_text(v.slug, v.legal_name)
    for s in u.staff:
        assert s.email.endswith("@maplerow.example")


def test_seed_refuses_a_tenant_the_universe_was_not_built_for() -> None:
    from typing import Any, cast

    import pytest
    from invoicedesk_data.seed import seed_into

    with pytest.raises(ValueError, match="cannot seed"):
        seed_into(cast(Any, None), build_universe(), tenant_id="other_tenant")
