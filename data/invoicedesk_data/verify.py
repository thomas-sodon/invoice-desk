"""Self-check: route gold truths through the real rules engine with idealized screening.

This answers "if extraction and screening were perfect, would every planted defect land where
the label says?" It is the label QA for the synthetic sets (tests run it on all of them) and a
reference for the evals component. It does not replace measuring the real pipeline.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from invoicedesk.config.tenant import RulesConfig, load_rules_config
from invoicedesk.contracts.extraction import HEADER_FIELDS, InvoiceExtraction
from invoicedesk.contracts.rules import DocumentInfo, RuleContext, RuleResult
from invoicedesk.contracts.screening import (
    ScreeningOutcome,
    ScreenResult,
    ScreenSpan,
    should_quarantine,
)
from invoicedesk.contracts.tenant_data import PriorInvoice, TenantData
from invoicedesk.rules import build_rules_engine
from invoicedesk.status import DocStatus, route

from invoicedesk_data.generator.truth import InvoiceTruth
from invoicedesk_data.gold import EmailFixture
from invoicedesk_data.paths import TENANTS_DIR
from invoicedesk_data.universe import AS_OF, TENANT_ID

__all__ = ["RouteCheck", "check_truth", "ideal_extraction", "ideal_screening", "prior_from"]


@dataclass(frozen=True)
class RouteCheck:
    doc_id: str
    expected: DocStatus
    got: DocStatus
    failed: dict[str, str]
    missing_rules: dict[str, str]  # planted rules that did not fail as labelled
    messages: list[str]

    @property
    def ok(self) -> bool:
        return self.expected == self.got and not self.missing_rules


def ideal_screening(truth: InvoiceTruth, email: EmailFixture | None = None) -> ScreeningOutcome:
    """What perfect detectors would report for this truth."""
    results: list[ScreenResult] = []
    for h in truth.hidden:
        detector = "faint_text" if h.style == "faint" else "hidden_text"
        results.append(
            ScreenResult(
                detector=detector,
                verdict="malicious",
                categories=[c for c in truth.expected.categories if c != "none"],  # type: ignore[misc]
                spans=[ScreenSpan(text=h.text, reason=h.style, source="pdf_text")],
            )
        )
    if email is not None and "injection_classifier" in truth.expected.detectors:
        cats = truth.expected.categories
        malicious = truth.expected.quarantined
        results.append(
            ScreenResult(
                detector="injection_classifier",
                verdict="malicious" if malicious else "suspicious",
                categories=list(cats),  # type: ignore[arg-type]
                spans=[ScreenSpan(text=email.body_text[:120], reason="email", source="email_body")],
            )
        )
    elif truth.doc_type == "bank_change_letter":
        results.append(
            ScreenResult(
                detector="injection_classifier",
                verdict="suspicious",
                categories=["bank_or_remit_change"],
                spans=[],
            )
        )
    return ScreeningOutcome(results=results, quarantined=should_quarantine(results))


def ideal_extraction(truth: InvoiceTruth) -> tuple[InvoiceExtraction, dict[str, float]]:
    """Gold extraction as a perfect reader would return it: illegible fields come back empty
    with low confidence; everything else is confident."""
    ex = truth.to_extraction()
    updates = {}
    for f in truth.illegible_fields:
        fld = getattr(ex, f)
        key = (
            "value_cents"
            if hasattr(fld, "value_cents")
            else ("value_iso" if hasattr(fld, "value_iso") else "value")
        )
        updates[f] = fld.model_copy(update={key: None, "confidence": 0.2})
    if updates:
        ex = ex.model_copy(update=updates)
    conf = {n: (0.2 if n in truth.illegible_fields else 0.99) for n in HEADER_FIELDS}
    return ex, conf


def prior_from(truth: InvoiceTruth, *, sha256: str | None = None) -> PriorInvoice:
    return PriorInvoice(
        source="document",
        ref_id=truth.doc_id,
        vendor_id=truth.vendor_id,
        invoice_number=truth.invoice_number,
        total_cents=truth.total_cents,
        invoice_date=truth.invoice_date,
        sha256=sha256,
    )


def check_truth(
    truth: InvoiceTruth,
    tenant: TenantData,
    *,
    config: RulesConfig | None = None,
    today: dt.date = AS_OF,
    email: EmailFixture | None = None,
    priors: Iterable[PriorInvoice] = (),
) -> RouteCheck:
    cfg = config or load_rules_config(TENANT_ID, TENANTS_DIR)
    td = tenant.model_copy(update={"prior_invoices": (*tenant.prior_invoices, *priors)})
    ex, conf = ideal_extraction(truth)
    ctx = RuleContext(
        extraction=ex,
        calibrated_conf=conf,
        screening=ideal_screening(truth, email),
        tenant=td,
        config=cfg,
        document=DocumentInfo(document_id=truth.doc_id, sha256=f"sha-{truth.doc_id}"),
        today=today,
        email_body_present=email is not None,
    )
    results: list[RuleResult] = build_rules_engine().evaluate(ctx)
    decision = route([r.outcome() for r in results])
    failed = {r.rule_id: r.severity for r in results if not r.passed}
    missing = {k: v for k, v in truth.expected.rules.items() if failed.get(k) != v}
    return RouteCheck(
        doc_id=truth.doc_id,
        expected=truth.expected.status,
        got=decision.target,
        failed=failed,
        missing_rules=missing,
        messages=[f"{r.rule_id}: {r.message}" for r in results if not r.passed],
    )
