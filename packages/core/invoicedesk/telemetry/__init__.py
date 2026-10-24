"""OpenTelemetry GenAI spans and cost accounting (M20).

* :func:`setup_tracing` installs an OTLP/HTTP exporter pointed at Langfuse when
  ``LANGFUSE_HOST`` + keys are configured; otherwise it does nothing (spans are then created by
  the OpenTelemetry API's no-op/default provider and cost nothing).
* :func:`llm_span` wraps one model call and emits GenAI semantic-convention attributes
  (``gen_ai.provider.name``/``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.usage.*``)
  plus ``invoicedesk.cost_usd``. Every string attribute passes through
  :func:`invoicedesk.masking.mask_text`, so bank and account numbers never reach a trace.
* :class:`CostMeter` sums per-call costs (``Decimal``) by tier, for batch cost meters.
"""

from __future__ import annotations

from invoicedesk.telemetry.spans import (
    CostMeter,
    LLMSpan,
    document_span,
    llm_span,
    noop_span,
    safe_attributes,
)
from invoicedesk.telemetry.tracing import langfuse_otlp_config, setup_tracing, shutdown_tracing

__all__ = [
    "CostMeter",
    "LLMSpan",
    "document_span",
    "langfuse_otlp_config",
    "llm_span",
    "noop_span",
    "safe_attributes",
    "setup_tracing",
    "shutdown_tracing",
]
