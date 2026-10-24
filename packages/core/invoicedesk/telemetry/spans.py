"""GenAI spans (OpenTelemetry semantic conventions for generative AI) with masking."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from invoicedesk.masking import mask_text

if TYPE_CHECKING:
    from invoicedesk.contracts.llm import LLMResponse

__all__ = [
    "TRACER_NAME",
    "CostMeter",
    "LLMSpan",
    "document_span",
    "llm_span",
    "noop_span",
    "safe_attributes",
]

TRACER_NAME = "invoicedesk"
AttrValue = str | bool | int | float


def safe_attributes(attributes: Mapping[str, Any]) -> dict[str, AttrValue]:
    """Coerce to OTel attribute types and mask long digit runs (invariant 9).

    ``None`` values are dropped; ``Decimal`` becomes its string form (costs are never floats
    in our own records, but a trace attribute is display-only); other objects are ``str()``-ed.
    """
    out: dict[str, AttrValue] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, Decimal):
            out[key] = str(value)
        else:
            out[key] = mask_text(str(value)) or ""
    return out


class LLMSpan:
    """Handle yielded by :func:`llm_span`; call :meth:`record` with the response."""

    def __init__(self, span: Span) -> None:
        self.span = span

    @property
    def trace_id(self) -> str | None:
        ctx = self.span.get_span_context()
        if not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")

    def set(self, **attributes: Any) -> None:
        self.span.set_attributes(safe_attributes(attributes))

    def record(self, response: LLMResponse) -> None:
        u = response.usage
        self.span.set_attributes(
            safe_attributes(
                {
                    "gen_ai.response.model": response.model_id,
                    "gen_ai.response.id": response.request_id,
                    "gen_ai.response.finish_reasons": response.stop_reason,
                    # Input tokens per the GenAI convention include cached ones.
                    "gen_ai.usage.input_tokens": u.input_tokens
                    + u.cache_read_input_tokens
                    + u.cache_creation_input_tokens,
                    "gen_ai.usage.output_tokens": u.output_tokens,
                    "gen_ai.usage.cache_read.input_tokens": u.cache_read_input_tokens,
                    "gen_ai.usage.cache_creation.input_tokens": u.cache_creation_input_tokens,
                    "invoicedesk.cost_usd": response.cost_usd,  # Decimal: kept verbatim
                    "invoicedesk.latency_ms": response.latency_ms,
                    "invoicedesk.refused": response.refused,
                }
            )
        )
        if response.refused:
            self.span.add_event("refusal", {"gen_ai.response.model": response.model_id})

    def error(self, kind: str, message: str) -> None:
        self.span.set_attribute("error.type", kind)
        self.span.set_status(Status(StatusCode.ERROR, mask_text(message) or ""))


@contextmanager
def llm_span(role: str, model_id: str, **attributes: Any) -> Iterator[LLMSpan]:
    """One ``chat {model}`` client span per model call (OTel GenAI conventions)."""
    tracer = trace.get_tracer(TRACER_NAME)
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.system": "anthropic",  # pre-1.37 convention name, still read by Langfuse
        "gen_ai.request.model": model_id,
        "invoicedesk.role": str(role),
        **attributes,
    }
    with tracer.start_as_current_span(
        f"chat {model_id}",
        kind=trace.SpanKind.CLIENT,
        attributes=safe_attributes(attrs),
        record_exception=False,  # exception messages may quote document text
    ) as span:
        yield LLMSpan(span)


@contextmanager
def document_span(name: str, **attributes: Any) -> Iterator[LLMSpan]:
    """An internal span (e.g. ``extract_and_score``) that parents the model-call spans."""
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(
        name, attributes=safe_attributes(attributes), record_exception=False
    ) as span:
        yield LLMSpan(span)


@contextmanager
def noop_span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    """Kept for callers written before telemetry existed."""
    yield dict(attributes)


class CostMeter:
    """Running ``Decimal`` USD totals, overall and per key (tier, role, document...)."""

    def __init__(self) -> None:
        self.total = Decimal(0)
        self.by_key: dict[str, Decimal] = {}

    def add(self, key: str, usd: Decimal | str) -> Decimal:
        amount = Decimal(usd)
        self.total += amount
        self.by_key[key] = self.by_key.get(key, Decimal(0)) + amount
        return self.total
