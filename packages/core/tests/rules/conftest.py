"""Shared fixtures for the rules tests: a small fictional Maple Row universe and a context factory.

Fixtures are session-scoped (everything is frozen) so hypothesis tests can use them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from invoicedesk.config.tenant import RulesConfig, load_rules_config
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.rules import DocumentInfo, RuleContext
from invoicedesk.contracts.screening import ScreeningOutcome
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
from invoicedesk.remit import remit_fingerprint
from invoicedesk.testing.builders import make_extraction

REPO = Path(__file__).resolve().parents[4]
TODAY = dt.date(2026, 10, 24)
# Reserved test routing number and a made-up account number (fictional data only).
MASTER_REMIT = "Routing 021000021 Acct 000123456789"
REQUIRED = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "work_order_number",
    "property_or_unit",
    "total",
)

RAPID = Vendor(
    id="v-rr",
    legal_name="Rapid Rooter Plumbing LLC",
    aliases=("Rapid Rooter",),
    address="118 Alder Way, Springfield",
    tax_id_last4="4821",
    remit_to_hash=remit_fingerprint(MASTER_REMIT),
    bank_last4="6789",
    phone_on_file="555-0100",
)
OAK = Vendor(
    id="v-oak",
    legal_name="Oakline Electric Inc",
    address="9 Birch Rd, Springfield",
    tax_id_last4="1177",
)
HARBOR = Vendor(id="v-pend", legal_name="Harbor Paint Co", status="pending")
NORTHGATE = Vendor(id="v-block", legal_name="Northgate Builders LLC", status="blocked")


def build_universe(**overrides: Any) -> TenantData:
    base: dict[str, Any] = {
        "tenant_id": "maple_row",
        "properties": (
            Property(id="p-maple", name="Maple Court", address="400 Elm St"),
            Property(id="p-birch", name="Birch Terrace", address="12 Birch Rd"),
        ),
        "units": (
            Unit(id="u-12b", property_id="p-maple", label="12B"),
            Unit(id="u-3a", property_id="p-birch", label="3A"),
        ),
        "vendors": (RAPID, OAK, HARBOR, NORTHGATE),
        "work_orders": (
            WorkOrder(
                id="wo-1187",
                wo_number="WO-1187",
                unit_id="u-12b",
                vendor_id="v-rr",
                scope="Kitchen drain repair",
                status="open",
                approved_estimate_cents=120_000,
                estimate_lines=(
                    EstimateLine(description="Replace kitchen drain trap", amount_cents=85_000),
                    EstimateLine(description="Labor", amount_cents=35_000),
                ),
            ),
            WorkOrder(
                id="wo-1190",
                wo_number="WO-1190",
                unit_id="u-3a",
                vendor_id="v-oak",
                scope="Panel upgrade",
                status="open",
                approved_estimate_cents=300_000,
            ),
            WorkOrder(
                id="wo-1001",
                wo_number="WO-1001",
                unit_id="u-12b",
                vendor_id="v-rr",
                scope="Old job",
                status="closed",
                approved_estimate_cents=50_000,
            ),
        ),
        "prior_invoices": (
            PriorInvoice(
                source="history",
                ref_id="hb-1",
                vendor_id="v-rr",
                invoice_number="INV-2100",
                total_cents=50_000,
                invoice_date=dt.date(2026, 6, 1),
                phash="ffff0000ffff0000",
            ),
        ),
    }
    base.update(overrides)
    return TenantData.model_validate(base)


CtxFactory = Callable[..., RuleContext]


@pytest.fixture(scope="session")
def universe() -> Callable[..., TenantData]:
    """``universe(**overrides) -> TenantData``."""
    return build_universe


@pytest.fixture(scope="session")
def config() -> RulesConfig:
    return load_rules_config("maple_row", REPO / "tenants")


@pytest.fixture(scope="session")
def make_ctx(config: RulesConfig) -> CtxFactory:
    def _make(
        extraction: InvoiceExtraction | None = None,
        *,
        tenant: TenantData | None = None,
        conf: dict[str, float] | None = None,
        screening: ScreeningOutcome | None = None,
        document: DocumentInfo | None = None,
        approvals: tuple[ApprovalRecord, ...] = (),
        cfg: RulesConfig | None = None,
        today: dt.date = TODAY,
        **ex_overrides: Any,
    ) -> RuleContext:
        ex = extraction or make_extraction(**ex_overrides)
        return RuleContext(
            extraction=ex,
            calibrated_conf=conf if conf is not None else dict.fromkeys(REQUIRED, 0.99),
            screening=screening or ScreeningOutcome(results=[], quarantined=False),
            tenant=tenant or build_universe(),
            config=cfg or config,
            document=document
            or DocumentInfo(
                document_id="d-new", sha256="sha-new", phash="0000ffff0000ffff", uploaded_by="u-up"
            ),
            today=today,
            approvals=approvals,
        )

    return _make
