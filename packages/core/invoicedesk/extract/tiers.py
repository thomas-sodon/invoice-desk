"""One tier = one independent, single-shot, tool-less structured-output call."""

from __future__ import annotations

import pydantic

from invoicedesk.config.models import TIER_ROLE, Tier, settings_for
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.extractor import TierRun
from invoicedesk.contracts.llm import ContentBlock, LLMCallError, LLMClient, LLMRequest, Usage
from invoicedesk.extract.prompts import extraction_system_blocks

__all__ = ["OUTPUT_SCHEMA", "build_tier_request", "run_tier"]

OUTPUT_SCHEMA = "invoicedesk.contracts.extraction.InvoiceExtraction"


def build_tier_request(
    tier: Tier, user: tuple[ContentBlock, ...], *, metadata: dict[str, str] | None = None
) -> LLMRequest:
    return LLMRequest(
        role=TIER_ROLE[tier],
        system=extraction_system_blocks(),
        user=user,
        output_schema=OUTPUT_SCHEMA,
        metadata={**(metadata or {}), "tier": tier},
    )


def run_tier(
    llm: LLMClient,
    tier: Tier,
    user: tuple[ContentBlock, ...],
    *,
    metadata: dict[str, str] | None = None,
) -> TierRun:
    """Run one tier. A refusal or an output that fails the schema yields ``output=None`` (the
    caller escalates). Transport errors (rate limit, connection, timeout, status) propagate so
    the job queue can retry the whole document."""
    req = build_tier_request(tier, user, metadata=metadata)
    try:
        resp = llm.parse(req, InvoiceExtraction)
    except LLMCallError as exc:
        if exc.kind != "validation":
            raise
        return TierRun(
            tier=tier,
            model_id=settings_for(req.role).model,
            output=None,
            refused=False,
            usage=Usage(),
            cost_usd="0",
            latency_ms=0,
        )
    output: InvoiceExtraction | None = None
    if not resp.refused and resp.parsed is not None:
        try:
            output = InvoiceExtraction.model_validate(resp.parsed)
        except pydantic.ValidationError:
            output = None
    return TierRun(
        tier=tier,
        model_id=resp.model_id,
        output=output,
        refused=resp.refused,
        usage=resp.usage,
        cost_usd=str(resp.cost_usd),
        latency_ms=resp.latency_ms,
        trace_id=resp.trace_id,
    )
