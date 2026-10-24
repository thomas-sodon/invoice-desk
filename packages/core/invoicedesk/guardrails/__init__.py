"""Guardrail screening (M8). STUB: owned by the `guardrails` component."""

from __future__ import annotations

from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import PreprocessedDocument
from invoicedesk.contracts.screening import Screener, ScreeningOutcome


def build_screeners(llm: LLMClient) -> list[Screener]:
    """hidden_text, faint_text and the Haiku injection classifier, in that order."""
    raise NotImplementedError("guardrails component not built yet")


def run_screening(
    doc: PreprocessedDocument, screeners: list[Screener], *, email_body: str | None
) -> ScreeningOutcome:
    """Run every screener and apply ``contracts.screening.should_quarantine``."""
    raise NotImplementedError("guardrails component not built yet")
