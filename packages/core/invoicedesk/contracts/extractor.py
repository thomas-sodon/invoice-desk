"""Extractor contract (M4). The extractor is tool-less (invariant 2) and runs only on documents
that screening did not quarantine (invariant 8)."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.llm import Usage
from invoicedesk.contracts.preprocess import PreprocessedDocument
from invoicedesk.contracts.screening import ScreeningOutcome

__all__ = ["ExtractionOutcome", "Extractor", "FieldResult", "TierRun"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TierRun(_M):
    """One model call; persisted as an ``extractions`` row."""

    tier: Literal["haiku", "sonnet", "opus"]
    model_id: str
    output: InvoiceExtraction | None
    refused: bool = False
    usage: Usage
    cost_usd: str  # Decimal as string
    latency_ms: int
    trace_id: str | None = None


class FieldResult(_M):
    """One ``fields`` row: final per-field value with calibrated confidence and location."""

    name: str
    value: str | None  # normalized string: cents as integer string, ISO dates
    raw_conf: float
    calibrated_conf: float
    word_ids: list[int] = Field(default_factory=list)
    box: tuple[int, int, int, int] | None = None
    page: int = 0


class ExtractionOutcome(_M):
    final: InvoiceExtraction
    fields: list[FieldResult]
    tiers: list[TierRun]
    escalated_to: Literal["haiku", "sonnet", "opus"] = "haiku"
    disagreements: list[str] = Field(default_factory=list)
    total_cost_usd: str = "0"


@runtime_checkable
class Extractor(Protocol):
    def extract(
        self,
        doc: PreprocessedDocument,
        *,
        screening: ScreeningOutcome,
        email_body: str | None = None,
        max_tier: Literal["haiku", "sonnet", "opus"] = "opus",
    ) -> ExtractionOutcome:
        """Tiered extraction + reconcile + calibration. Must refuse (raise ValueError) when
        ``screening.quarantined`` is True."""
        ...
