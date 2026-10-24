"""Rules-engine contract (M7, R1-R10). Rules are pure functions of a :class:`RuleContext`
(CLAUDE.md invariant 7); routing is :func:`invoicedesk.status.route`."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.config.tenant import RulesConfig
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.contracts.tenant_data import ApprovalRecord, TenantData
from invoicedesk.status import RuleOutcome

__all__ = ["DocumentInfo", "Rule", "RuleContext", "RuleResult", "RulesEngine"]

Severity = Literal["pass", "review", "flag"]


class DocumentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    sha256: str
    phash: str | None = None
    uploaded_by: str | None = None
    source: Literal["upload", "email", "seed", "mcp"] = "upload"


class RuleContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    extraction: InvoiceExtraction
    calibrated_conf: dict[str, float]
    screening: ScreeningOutcome
    tenant: TenantData
    config: RulesConfig
    document: DocumentInfo
    today: dt.date  # injected, never read from the clock inside a rule
    approvals: tuple[ApprovalRecord, ...] = ()
    #: Text of the email body, if any (classifier categories already in ``screening``).
    email_body_present: bool = False


class RuleResult(BaseModel):
    """One ``rule_results`` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str  # "R1".."R10"
    name: str  # e.g. "vendor_known"
    passed: bool
    severity: Severity  # "pass" when passed
    message: str  # plain-English, shown in the UI (bank numbers masked)
    #: Fields to highlight in the viewer (amber/red), e.g. ["total"].
    fields: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    def outcome(self) -> RuleOutcome:
        return RuleOutcome(self.rule_id, self.passed, self.severity, self.message)


@runtime_checkable
class Rule(Protocol):
    rule_id: str
    name: str

    def __call__(self, ctx: RuleContext) -> RuleResult: ...


@runtime_checkable
class RulesEngine(Protocol):
    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        """Run R1..R10 (always all ten, in order)."""
        ...
