"""Tiered extraction (M4): Haiku -> Sonnet -> Opus, deterministic reconcile, calibration.

The schema lives in ``invoicedesk.contracts.extraction`` (shared contract). The extractor is
tool-less and only reads; every decision is made downstream by code.
"""

from __future__ import annotations

from invoicedesk.config.tenant import ConfidenceConfig
from invoicedesk.contracts.calibration import Calibrator
from invoicedesk.contracts.extractor import Extractor
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.extract.extractor import (
    ExtractionFailed,
    QuarantinedDocument,
    TieredExtractor,
    field_results,
)
from invoicedesk.extract.files_api import PageImageUploader
from invoicedesk.extract.reconcile import ReconcileResult, TierOutput, reconcile

__all__ = [
    "ExtractionFailed",
    "PageImageUploader",
    "QuarantinedDocument",
    "ReconcileResult",
    "TierOutput",
    "TieredExtractor",
    "build_extractor",
    "field_results",
    "reconcile",
]


def build_extractor(
    llm: LLMClient,
    *,
    calibrator: Calibrator,
    storage: Storage,
    confidence: ConfidenceConfig | None = None,
    rounding_slack_cents: int = 1,
) -> Extractor:
    """The production extractor. ``confidence`` carries the tenant's ``tau_field``/``tau_hi``
    and required fields (``RulesConfig.confidence``); defaults match ``rules.yaml``."""
    return TieredExtractor(
        llm,
        calibrator=calibrator,
        storage=storage,
        confidence=confidence,
        rounding_slack_cents=rounding_slack_cents,
    )
