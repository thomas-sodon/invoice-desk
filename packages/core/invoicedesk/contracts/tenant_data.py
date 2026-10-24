"""Business-universe records the rules engine reads (DESIGN §6.1). Loaded from Postgres by
``invoicedesk.db.repo.load_tenant_data`` or built directly in tests. Money is integer cents."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.money import Cents

__all__ = [
    "ApprovalRecord",
    "EstimateLine",
    "PriorInvoice",
    "Property",
    "TenantData",
    "Unit",
    "Vendor",
    "WorkOrder",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Property(_M):
    id: str
    name: str
    address: str


class Unit(_M):
    id: str
    property_id: str
    label: str  # e.g. "12B"


class Vendor(_M):
    id: str
    legal_name: str
    aliases: tuple[str, ...] = ()
    address: str = ""
    tax_id_last4: str | None = None
    remit_to_hash: str | None = None  # invoicedesk.remit.remit_fingerprint of remit details
    bank_last4: str | None = None
    phone_on_file: str | None = None
    email: str | None = None
    status: Literal["approved", "pending", "blocked"] = "approved"


class EstimateLine(_M):
    description: str
    amount_cents: Cents


class WorkOrder(_M):
    id: str
    wo_number: str
    unit_id: str
    vendor_id: str
    scope: str
    status: Literal["open", "closed"]
    approved_estimate_cents: Cents
    estimate_lines: tuple[EstimateLine, ...] = ()


class PriorInvoice(_M):
    """Historical bill or previously processed document, for R4 duplicate detection."""

    source: Literal["history", "document"]
    ref_id: str  # historical bill id or document id
    vendor_id: str | None
    invoice_number: str | None
    total_cents: Cents | None
    invoice_date: dt.date | None
    phash: str | None = None
    sha256: str | None = None
    status: str | None = None


class ApprovalRecord(_M):
    approver_id: str
    kind: Literal["bank_change", "over_cap", "new_vendor", "duplicate", "injection", "other"]
    decision: Literal["approve", "reject"]
    callback_attested: bool = False


class TenantData(_M):
    tenant_id: str
    properties: tuple[Property, ...] = ()
    units: tuple[Unit, ...] = ()
    vendors: tuple[Vendor, ...] = ()
    work_orders: tuple[WorkOrder, ...] = ()
    prior_invoices: tuple[PriorInvoice, ...] = Field(default=())

    def vendor(self, vendor_id: str) -> Vendor | None:
        return next((v for v in self.vendors if v.id == vendor_id), None)

    def work_order(self, wo_number: str) -> WorkOrder | None:
        return next((w for w in self.work_orders if w.wo_number == wo_number), None)

    def unit(self, unit_id: str) -> Unit | None:
        return next((u for u in self.units if u.id == unit_id), None)
