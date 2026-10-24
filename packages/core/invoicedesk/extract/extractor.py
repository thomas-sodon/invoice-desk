"""Tiered extractor (M4) implementing the DESIGN §5.5 ``extract_and_score`` routing.

::

    t1 = extract(haiku)
    conf = calibrate(t1)
    if needs_escalation(conf, t1):        # required field < tau_field, arithmetic fail,
        t2 = extract(sonnet)              # handwriting_present, or a Haiku refusal
        merged = reconcile(t1, t2)
        if disagreement on a required field or min required conf < tau_hi:
            t3 = extract(opus)            # adjudicate; still no tools
            merged = adjudicate(t1, t2, t3)
    final = calibrate(merged)

Every tier is an independent single-shot call over the same frozen prefix and the same user
turn. The extractor refuses quarantined documents (invariant 8) and has no tools (invariant 2).
It only *reads*: routing to ReadyToPay/NeedsReview/Flagged is the rules engine's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

from invoicedesk.calibrate.features import build_field_signals, iter_scored_fields
from invoicedesk.config.models import Tier
from invoicedesk.config.tenant import ConfidenceConfig
from invoicedesk.contracts.calibration import Calibrator
from invoicedesk.contracts.extraction import (
    DateField,
    InvoiceExtraction,
    MoneyField,
)
from invoicedesk.contracts.extractor import ExtractionOutcome, FieldResult, TierRun
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import ContentBlock, LLMClient
from invoicedesk.contracts.preprocess import Box, OcrWord, PreprocessedDocument
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.extract.arithmetic import arithmetic_failed, arithmetic_signals
from invoicedesk.extract.files_api import PageImageUploader
from invoicedesk.extract.reconcile import ReconcileResult, TierOutput, reconcile
from invoicedesk.extract.tiers import run_tier
from invoicedesk.extract.userturn import build_user_turn
from invoicedesk.telemetry import document_span

__all__ = [
    "EscalationDecision",
    "ExtractionFailed",
    "QuarantinedDocument",
    "TieredExtractor",
    "field_results",
]

_ORDER: dict[Tier, int] = {"haiku": 0, "sonnet": 1, "opus": 2}


class QuarantinedDocument(ValueError):
    """Screening quarantined this document; it must never reach any extraction tier."""


class ExtractionFailed(RuntimeError):
    """No tier produced a schema-valid output (refusals/invalid JSON at every allowed tier).
    ``tiers`` still carries the calls made, so their cost can be recorded."""

    def __init__(self, message: str, tiers: list[TierRun]) -> None:
        super().__init__(message)
        self.tiers = tiers


class EscalationDecision:
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons

    def __bool__(self) -> bool:
        return bool(self.reasons)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EscalationDecision({self.reasons!r})"


def _normalized(f: object) -> str | None:
    if isinstance(f, MoneyField):
        return None if f.value_cents is None else str(f.value_cents)
    if isinstance(f, DateField):
        return f.value_iso
    return getattr(f, "value", None)


def _union_box(words: list[OcrWord]) -> Box:
    return (
        min(w.box[0] for w in words),
        min(w.box[1] for w in words),
        max(w.box[2] for w in words),
        max(w.box[3] for w in words),
    )


def field_results(
    ex: InvoiceExtraction,
    calibrated: Mapping[str, float],
    words_by_id: Mapping[int, OcrWord],
    *,
    doc_type_conf: float,
) -> list[FieldResult]:
    """``fields`` rows: header fields, ``doc_type`` and every line-item cell.

    Boxes are the union of the cited OCR words' boxes on the page that holds most of them; the
    model's ``fallback_box`` is used only when the field cites no valid OCR word (handwriting).
    """
    rows = [
        FieldResult(
            name="doc_type",
            value=ex.doc_type,
            raw_conf=1.0,
            calibrated_conf=doc_type_conf,
        )
    ]
    for name, f in iter_scored_fields(ex):
        words = [words_by_id[i] for i in f.source.word_ids if i in words_by_id]
        box: Box | None = None
        page = 0
        if words:
            pages: dict[int, int] = {}
            for w in words:
                pages[w.page] = pages.get(w.page, 0) + 1
            page = max(pages, key=lambda p: (pages[p], -p))
            box = _union_box([w for w in words if w.page == page])
        elif f.source.fallback_box is not None and len(f.source.fallback_box) == 4:
            x0, y0, x1, y1 = (max(0, int(v)) for v in f.source.fallback_box)
            if x1 > x0 and y1 > y0:
                box = (x0, y0, x1, y1)
        rows.append(
            FieldResult(
                name=name,
                value=_normalized(f),
                raw_conf=f.confidence,
                calibrated_conf=float(calibrated.get(name, 0.0)),
                word_ids=[w.id for w in words],
                box=box,
                page=page,
            )
        )
    return rows


class TieredExtractor:
    def __init__(
        self,
        llm: LLMClient,
        *,
        calibrator: Calibrator,
        storage: Storage,
        confidence: ConfidenceConfig | None = None,
        rounding_slack_cents: int = 1,
        uploader: PageImageUploader | None = None,
    ) -> None:
        self.llm = llm
        self.calibrator = calibrator
        self.confidence = confidence or ConfidenceConfig()
        self.slack = rounding_slack_cents
        self.uploader = uploader or PageImageUploader(llm, storage)

    # ------------------------------------------------------------------ scoring
    def _calibrate(
        self,
        ex: InvoiceExtraction,
        words_by_id: Mapping[int, OcrWord],
        agreement: Mapping[str, bool] | None,
    ) -> dict[str, float]:
        signals = build_field_signals(
            ex,
            words_by_id,
            arithmetic=arithmetic_signals(ex, slack_cents=self.slack),
            agreement=agreement,
        )
        return self.calibrator.calibrate(signals)

    def needs_escalation(
        self, ex: InvoiceExtraction | None, calibrated: Mapping[str, float], *, refused: bool
    ) -> EscalationDecision:
        if ex is None:
            return EscalationDecision(["haiku_refused" if refused else "haiku_invalid_output"])
        reasons = [
            f"low_confidence:{name}"
            for name in self.confidence.required_fields
            if calibrated.get(name, 0.0) < self.confidence.threshold(name)
        ]
        if arithmetic_failed(ex, slack_cents=self.slack):
            reasons.append("arithmetic_failed")
        if ex.handwriting_present:
            reasons.append("handwriting_present")
        return EscalationDecision(reasons)

    def min_required_conf(self, calibrated: Mapping[str, float]) -> float:
        return min((calibrated.get(n, 0.0) for n in self.confidence.required_fields), default=0.0)

    # ------------------------------------------------------------------ main
    def extract(
        self,
        doc: PreprocessedDocument,
        *,
        screening: ScreeningOutcome,
        email_body: str | None = None,
        max_tier: Literal["haiku", "sonnet", "opus"] = "opus",
    ) -> ExtractionOutcome:
        if screening.quarantined:
            raise QuarantinedDocument(
                f"document {doc.document_id} is quarantined by screening; extraction refused"
            )
        with document_span(
            "extract_and_score", **{"invoicedesk.document_id": doc.document_id}
        ) as span:
            outcome = self._extract(doc, screening, email_body, max_tier)
            span.set(
                **{
                    "invoicedesk.escalated_to": outcome.escalated_to,
                    "invoicedesk.cost_usd": Decimal(outcome.total_cost_usd),
                    "invoicedesk.disagreements": ",".join(outcome.disagreements),
                }
            )
            return outcome

    def _extract(
        self,
        doc: PreprocessedDocument,
        screening: ScreeningOutcome,
        email_body: str | None,
        max_tier: Tier,
    ) -> ExtractionOutcome:
        words_by_id = {w.id: w for w in doc.words}
        images = self.uploader.image_blocks(doc)
        user: tuple[ContentBlock, ...] = build_user_turn(images, doc, screening, email_body)
        meta = {"document_id": doc.document_id}
        runs: list[TierRun] = []

        def call(tier: Tier) -> TierRun:
            run = run_tier(self.llm, tier, user, metadata=meta)
            runs.append(run)
            return run

        t1 = call("haiku")
        cal1 = self._calibrate(t1.output, words_by_id, None) if t1.output else {}
        decision = self.needs_escalation(t1.output, cal1, refused=t1.refused)

        result: ReconcileResult | None = (
            ReconcileResult(merged=t1.output) if t1.output is not None else None
        )
        if decision and _ORDER[max_tier] >= _ORDER["sonnet"]:
            t2 = call("sonnet")
            outs = [TierOutput(r.tier, r.output) for r in (t1, t2) if r.output is not None]
            if outs:
                result = reconcile(outs)
                cal2 = self._calibrate(result.merged, words_by_id, result.agreement or None)
                required = set(self.confidence.required_fields)
                disagree_required = any(f in required for f in result.disagreements)
                if _ORDER[max_tier] >= _ORDER["opus"] and (
                    disagree_required or self.min_required_conf(cal2) < self.confidence.tau_hi
                ):
                    t3 = call("opus")
                    outs3 = [
                        TierOutput(r.tier, r.output) for r in (t1, t2, t3) if r.output is not None
                    ]
                    result = reconcile(outs3)

        if result is None:
            raise ExtractionFailed(
                "no tier produced a valid extraction "
                f"({', '.join(f'{r.tier}:{"refused" if r.refused else "invalid"}' for r in runs)})",
                runs,
            )

        final = result.merged
        agreement = result.agreement or None
        calibrated = self._calibrate(final, words_by_id, agreement)
        doc_type_conf = 0.5 if "doc_type" in result.disagreements else 1.0
        total = sum((Decimal(r.cost_usd) for r in runs), Decimal(0))
        return ExtractionOutcome(
            final=final,
            fields=field_results(final, calibrated, words_by_id, doc_type_conf=doc_type_conf),
            tiers=runs,
            escalated_to=runs[-1].tier,
            disagreements=list(result.disagreements),
            total_cost_usd=str(total),
        )
