"""Shared helpers for the evals tests (no network, no paid APIs)."""

from __future__ import annotations

from typing import Any

import pytest
from invoicedesk.testing.builders import make_extraction
from invoicedesk_evals.model import DocScore, FieldScore


@pytest.fixture
def clean_extraction() -> Any:
    return make_extraction()


@pytest.fixture
def make_score() -> Any:
    return doc_score


def doc_score(**kw: Any) -> DocScore:
    """A DocScore with neutral defaults (a clean, correctly auto-posted invoice)."""
    base: dict[str, Any] = {
        "example_id": "synthetic-core/core-x",
        "doc_id": "core-x",
        "set": "synthetic-core",
        "split": "dev",
        "medium": "native",
        "category": None,
        "config": "D",
        "repeat": 0,
        "status": "ReadyToPay",
        "expected_status": "ReadyToPay",
        "auto_posted": True,
        "clean_truth": True,
        "quarantined": False,
        "expected_quarantined": False,
        "extracted": True,
        "required_correct": True,
        "rules_scored": [f"R{i}" for i in range(1, 11)],
        "fields": {"total": FieldScore(tp=1, fp=0, fn=0, correct=True, gold=100, pred=100)},
        "cost_usd": "0.01",
        "latency_ms": 3000,
    }
    base.update(kw)
    return DocScore(**base)
