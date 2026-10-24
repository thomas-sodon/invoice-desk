"""Guardrail screening contract (DESIGN §5.5, M8; CLAUDE.md invariant 8).

Screening runs BEFORE extraction. :func:`should_quarantine` is the deterministic R10 policy the
pipeline applies to the screen results.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.contracts.preprocess import Box, PreprocessedDocument

__all__ = [
    "InjectionCategory",
    "InjectionVerdict",
    "ScreenResult",
    "ScreenSpan",
    "Screener",
    "ScreeningOutcome",
    "Verdict",
    "should_quarantine",
]

Verdict = Literal["clean", "suspicious", "malicious"]
InjectionCategory = Literal[
    "approval_override",
    "bank_or_remit_change",
    "instruction_to_ai",
    "urgency_pressure",
    "identity_mismatch",
    "none",
]
Detector = Literal["hidden_text", "faint_text", "injection_classifier"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenSpan(_M):
    text: str
    reason: str  # e.g. "fill #FFFFFF on #FFFFFF", "font 2.0pt", "off-page", category name
    page: int | None = None
    box: Box | None = None
    source: Literal["pdf_text", "ocr", "ocr_enhanced", "email_body"] = "ocr"


class ScreenResult(_M):
    detector: Detector
    verdict: Verdict
    categories: list[InjectionCategory] = Field(default_factory=list)
    spans: list[ScreenSpan] = Field(default_factory=list)
    cost_usd: str = "0"  # Decimal as string (classifier only)


class InjectionVerdict(_M):
    """Structured output of the Haiku injection/bank-change classifier (§5.4)."""

    class Span(_M):
        category: InjectionCategory
        text: str

    verdict: Verdict
    categories: list[InjectionCategory]
    spans: list[Span]


class ScreeningOutcome(_M):
    results: list[ScreenResult]
    quarantined: bool

    @property
    def categories(self) -> set[str]:
        return {c for r in self.results for c in r.categories if c != "none"}

    @property
    def flagged_spans(self) -> list[ScreenSpan]:
        return [s for r in self.results if r.verdict != "clean" for s in r.spans]


def should_quarantine(results: list[ScreenResult]) -> bool:
    """R10 policy: any hidden/faint-text finding, or a ``malicious`` classifier verdict."""
    for r in results:
        if r.detector in ("hidden_text", "faint_text") and r.verdict != "clean":
            return True
        if r.detector == "injection_classifier" and r.verdict == "malicious":
            return True
    return False


@runtime_checkable
class Screener(Protocol):
    name: str

    def screen(self, doc: PreprocessedDocument, *, email_body: str | None) -> ScreenResult: ...
