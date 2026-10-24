"""Truth sampler, gold labels, frozen split and the rules self-check."""

from __future__ import annotations

import itertools
from collections import Counter

import pytest
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.money import apply_bps
from invoicedesk_data.adversarial import CATEGORY_COUNTS, adversarial_items
from invoicedesk_data.demo import demo_plan
from invoicedesk_data.frozen import self_check, verify_frozen
from invoicedesk_data.generator.sampler import CollisionRegistry, invoice_key
from invoicedesk_data.gold import load_gold, load_manifest
from invoicedesk_data.splits import CORE_COUNTS, CORE_DEFECTS, assign_splits, core_truths
from invoicedesk_data.universe import build_universe


@pytest.fixture(scope="module")
def truths():
    return core_truths()


def test_core_is_deterministic(truths) -> None:
    assert [t.model_dump() for t in truths] == [t.model_dump() for t in core_truths()]


def test_core_stratification(truths) -> None:
    assert len(truths) == 120
    assert Counter(t.medium for t in truths) == Counter(CORE_COUNTS)
    planted = Counter(d for t in truths for d in t.defects)
    assert planted == Counter(CORE_DEFECTS)
    assert all(t.medium == "handwritten" for t in truths if "illegible_total" in t.defects)
    splits = assign_splits(truths)
    assert Counter(splits.values()) == Counter({"dev": 60, "test": 60})
    for medium in CORE_COUNTS:
        ids = [t.doc_id for t in truths if t.medium == medium]
        dev = sum(splits[i] == "dev" for i in ids)
        assert abs(dev - (len(ids) - dev)) <= 1


def test_truth_arithmetic_is_integer_and_consistent(truths) -> None:
    for t in truths:
        for ln in t.lines:
            assert isinstance(ln.amount_cents, int)
            assert ln.amount_cents == ln.unit_price_cents * int(ln.quantity)
        assert t.subtotal_cents == sum(ln.amount_cents for ln in t.lines)
        assert t.tax_cents == apply_bps(t.subtotal_cents or 0, t.tax_rate_bps)
        if "arithmetic" in t.defects:
            assert t.total_cents != (t.subtotal_cents or 0) + (t.tax_cents or 0)
        else:
            assert t.total_cents == (t.subtotal_cents or 0) + (t.tax_cents or 0)
        InvoiceExtraction.model_validate(t.to_extraction().model_dump())


def test_no_unintended_duplicates_within_or_across_sets(truths) -> None:
    u = build_universe()
    reg = CollisionRegistry()
    for h in u.historical_bills:
        reg.add(h.vendor_id, h.invoice_number, h.total_cents, h.invoice_date, h.id)
    plan, att = demo_plan(u)
    demo = [p.truth for p in plan if "duplicate" not in p.truth.defects] + [att.truth]
    for t in [*truths, *demo]:
        assert reg.collides(t) is None, t.doc_id
        reg.add_truth(t)


def test_invoice_key_matches_r4_normalization() -> None:
    from invoicedesk.rules._text import invoice_number_key

    for v in ("INV-002231", "inv 2231", "#2231", "A-3600", "26-0042", "MR1043", "1043-B", None):
        assert invoice_key(v) == invoice_number_key(v)


def test_adversarial_composition() -> None:
    items = adversarial_items()
    cats = Counter(i.category for i in items if i.category != "prior")
    assert cats == Counter(CATEGORY_COUNTS)
    assert sum(cats.values()) == 30
    ids = {i.truth.doc_id for i in items}
    for i in items:
        assert set(i.priors) <= ids
        if i.category != "prior":
            assert str(i.truth.expected.status) == "Flagged"


def test_frozen_split_is_intact() -> None:
    assert verify_frozen() == []


@pytest.mark.parametrize("set_name", ["synthetic-core", "adversarial", "demo"])
def test_gold_routes_as_labelled_through_the_real_rules_engine(set_name: str) -> None:
    checks = self_check(set_name)
    bad = [(c.doc_id, c.expected, c.got, c.messages[:2]) for c in checks if not c.ok]
    assert bad == []


@pytest.mark.parametrize("set_name", ["synthetic-core", "adversarial", "demo"])
def test_gold_records_are_schema_valid_with_boxes(set_name: str) -> None:
    m = load_manifest(set_name)
    records = load_gold(set_name)
    assert len(records) == m.count
    for r in records:
        ex = r.invoice_extraction()
        assert r.gold_sha256() == next(e.gold_sha256 for e in m.entries if e.id == r.id)
        if r.truth.doc_type == "invoice" and r.truth.invoice_number:
            assert "invoice_number" in r.boxes
            assert ex.invoice_number.source.fallback_box == list(r.boxes["invoice_number"].box)
        for b in r.boxes.values():
            x0, y0, x1, y1 = b.box
            assert 0 <= x0 < x1 and 0 <= y0 < y1 and x1 <= 1600 and y1 <= 1600


def test_demo_manifest_excluded_from_eval_splits() -> None:
    core = load_manifest("synthetic-core")
    demo = load_manifest("demo")
    core_files = {e.sha256 for e in core.entries}
    assert not core_files & {e.sha256 for e in demo.entries}
    assert {e.split for e in demo.entries} == {"demo"}


def test_pairwise_distinct_core_files() -> None:
    m = load_manifest("synthetic-core")
    for a, b in itertools.combinations(m.entries, 2):
        assert a.sha256 != b.sha256
