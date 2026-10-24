"""Tiered extraction (M4). STUB: owned by the `extraction` component.
The schema lives in ``invoicedesk.contracts.extraction`` (shared contract)."""

from __future__ import annotations

from invoicedesk.contracts.calibration import Calibrator
from invoicedesk.contracts.extractor import Extractor
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMClient


def build_extractor(llm: LLMClient, *, calibrator: Calibrator, storage: Storage) -> Extractor:
    raise NotImplementedError("extraction component not built yet")
