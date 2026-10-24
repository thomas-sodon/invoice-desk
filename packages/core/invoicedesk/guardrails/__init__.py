"""Guardrail screening (DESIGN §5.5 pre-LLM and LLM layers, R10, M8; CLAUDE.md invariants 2, 8, 9).

Screening runs before extraction. Three screeners, in order:

1. :class:`~.hidden_text.HiddenTextScreener` (deterministic): PDF text-layer words that are
   off-page, under 4 pt, within ΔE < 5 of their background, or absent from OCR of the render;
2. :class:`~.faint_text.FaintTextScreener` (deterministic): text only the CLAHE-enhanced OCR
   pass can read;
3. :class:`~.classifier.InjectionClassifier`: Haiku 5.5 over all document text, revealed
   hidden/faint text and the email body, via the ``LLMClient`` Protocol.

:func:`run_screening` applies the R10 policy
:func:`~invoicedesk.contracts.screening.should_quarantine`.
Every span is masked (bank/account numbers to the last 4) and keeps the revealed text, so a
quarantined document's hidden text is visible to the reviewer and never silently dropped.

``storage`` (optional) lets the deterministic detectors look at the stored page renders to tell
"visible text the OCR engine missed" (handwriting, a framed line) from hidden or faint text.
Without it they fall back to text-only evidence, which can only flag more.
"""

from __future__ import annotations

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMCallError, LLMClient
from invoicedesk.contracts.preprocess import PreprocessedDocument
from invoicedesk.contracts.screening import (
    Screener,
    ScreeningOutcome,
    ScreenResult,
    ScreenSpan,
    should_quarantine,
)
from invoicedesk.guardrails.classifier import InjectionClassifier
from invoicedesk.guardrails.faint_text import FaintTextScreener, find_faint_text
from invoicedesk.guardrails.hidden_text import HiddenTextScreener, find_hidden_text

__all__ = [
    "FaintTextScreener",
    "HiddenTextScreener",
    "InjectionClassifier",
    "build_screeners",
    "find_faint_text",
    "find_hidden_text",
    "run_screening",
]

_DETERMINISTIC = ("hidden_text", "faint_text")


def build_screeners(llm: LLMClient, *, storage: Storage | None = None) -> list[Screener]:
    """hidden_text, faint_text and the Haiku injection classifier, in that order."""
    return [
        HiddenTextScreener(storage),
        FaintTextScreener(storage),
        InjectionClassifier(llm, storage=storage),
    ]


def run_screening(
    doc: PreprocessedDocument, screeners: list[Screener], *, email_body: str | None
) -> ScreeningOutcome:
    """Run every screener and apply ``contracts.screening.should_quarantine``.

    A deterministic detector that crashes fails closed: its result is ``suspicious`` (which
    quarantines). Classifier transport errors propagate so the caller retries the job; a
    document is never reported as screened when a screener did not run."""
    results: list[ScreenResult] = []
    for screener in screeners:
        try:
            results.append(screener.screen(doc, email_body=email_body))
        except LLMCallError:
            raise
        except Exception as exc:
            if screener.name not in _DETERMINISTIC:
                raise
            results.append(
                ScreenResult(
                    detector=screener.name,  # type: ignore[arg-type]
                    verdict="suspicious",
                    categories=["none"],
                    spans=[
                        ScreenSpan(
                            text=f"detector failed ({type(exc).__name__}); quarantined to be safe",
                            reason="detector_error",
                        )
                    ],
                )
            )
    return ScreeningOutcome(results=results, quarantined=should_quarantine(results))
