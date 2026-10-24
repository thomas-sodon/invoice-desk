"""GenAI spans with masking, Langfuse OTLP config, cost meter."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from decimal import Decimal

import pytest
from invoicedesk.config.models import HAIKU, ModelRole
from invoicedesk.config.settings import Settings
from invoicedesk.contracts.llm import LLMResponse, Usage
from invoicedesk.telemetry import (
    CostMeter,
    document_span,
    langfuse_otlp_config,
    llm_span,
    noop_span,
    safe_attributes,
    setup_tracing,
    shutdown_tracing,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    # get_tracer() is resolved per call, so pointing it at a private provider is enough
    monkeypatch.setattr(trace, "get_tracer", _PROVIDER.get_tracer)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def _settings(**kw: object) -> Settings:
    return Settings.model_validate(kw)


def _response(stop: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        role=ModelRole.EXTRACT_T1,
        model_id=HAIKU,
        stop_reason=stop,
        parsed={} if stop != "refusal" else None,
        usage=Usage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=600,
            cache_creation_input_tokens=10,
        ),
        cost_usd=Decimal("0.00012345"),
        latency_ms=42,
        request_id="msg_1",
    )


def test_llm_span_genai_attributes(spans: InMemorySpanExporter) -> None:
    with llm_span(ModelRole.EXTRACT_T1, HAIKU, **{"invoicedesk.document_id": "doc-1"}) as s:
        assert s.trace_id is not None and len(s.trace_id) == 32
        s.record(_response())
    (span,) = spans.get_finished_spans()
    a = dict(span.attributes or {})
    assert span.name == f"chat {HAIKU}"
    assert span.kind == trace.SpanKind.CLIENT
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "anthropic"
    assert a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.request.model"] == HAIKU
    assert a["gen_ai.response.model"] == HAIKU
    assert a["gen_ai.usage.input_tokens"] == 710
    assert a["gen_ai.usage.output_tokens"] == 50
    assert a["gen_ai.usage.cache_read.input_tokens"] == 600
    assert a["invoicedesk.cost_usd"] == "0.00012345"
    assert a["invoicedesk.role"] == "extract_t1"
    assert a["invoicedesk.document_id"] == "doc-1"


def test_bank_numbers_are_masked_in_spans(spans: InMemorySpanExporter) -> None:
    with llm_span("wording", HAIKU, note="remit to acct 000123456789") as s:
        s.set(detail="routing 021000021")
        s.error("status", "400 for account 9876543210")
    (span,) = spans.get_finished_spans()
    blob = repr(dict(span.attributes or {})) + repr(span.status.description)
    for raw in ("000123456789", "021000021", "9876543210"):
        assert raw not in blob
    assert "6789" in blob
    assert span.status.status_code == trace.StatusCode.ERROR


def test_refusal_event_and_document_span(spans: InMemorySpanExporter) -> None:
    with document_span("extract_and_score", doc="d1") as parent:
        with llm_span("extract_t1", HAIKU) as s:
            s.record(_response("refusal"))
        parent.set(escalated_to="sonnet")
    child, root = spans.get_finished_spans()
    assert [e.name for e in child.events] == ["refusal"]
    assert child.parent is not None and root.context is not None
    assert child.parent.span_id == root.context.span_id
    assert dict(root.attributes or {})["escalated_to"] == "sonnet"


def test_safe_attributes_types() -> None:
    out = safe_attributes(
        {
            "a": None,
            "b": True,
            "c": 3,
            "d": 1.5,
            "e": Decimal("0.1"),
            "f": ["x"],
            "g": "acct 12345678",
        }
    )
    assert "a" not in out
    assert out["b"] is True
    assert out["c"] == 3
    assert out["e"] == "0.1"
    assert out["f"] == "['x']"
    assert "12345678" not in str(out["g"])


def test_noop_span_and_cost_meter() -> None:
    with noop_span("x", a=1) as attrs:
        assert attrs == {"a": 1}
    m = CostMeter()
    m.add("haiku", Decimal("0.0009"))
    m.add("sonnet", "0.023")
    m.add("haiku", Decimal("0.0009"))
    assert m.total == Decimal("0.0248")
    assert m.by_key["haiku"] == Decimal("0.0018")


def test_langfuse_otlp_config() -> None:
    assert langfuse_otlp_config(_settings()) is None
    s = _settings(
        LANGFUSE_HOST="https://lf.example.test/",
        LANGFUSE_PUBLIC_KEY="pk-lf-1",
        LANGFUSE_SECRET_KEY=SecretStr("sk-lf-2"),
    )
    cfg = langfuse_otlp_config(s)
    assert cfg is not None
    endpoint, headers = cfg
    assert endpoint == "https://lf.example.test/api/public/otel/v1/traces"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode()


def test_setup_tracing_is_noop_offline_or_unconfigured() -> None:
    shutdown_tracing()
    assert setup_tracing(_settings()) is None
    offline = _settings(
        INVOICEDESK_MODE="offline",
        LANGFUSE_HOST="https://lf.example.test",
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY=SecretStr("sk"),
    )
    assert setup_tracing(offline) is None  # offline never exports to the network


def test_setup_tracing_live_builds_otlp_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    shutdown_tracing()
    live = _settings(
        INVOICEDESK_MODE="live",
        LANGFUSE_HOST="https://lf.example.test",
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY=SecretStr("sk"),
    )
    provider = setup_tracing(live)
    try:
        assert provider is not None
        assert installed == [provider]
        assert setup_tracing(live) is provider  # idempotent
    finally:
        shutdown_tracing()


def test_setup_tracing_with_explicit_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: None)
    shutdown_tracing()
    exporter = InMemorySpanExporter()
    provider = setup_tracing(_settings(), exporter=exporter)
    try:
        assert provider is not None
        with provider.get_tracer("t").start_as_current_span("x"):
            pass
        provider.force_flush()
        assert [s.name for s in exporter.get_finished_spans()] == ["x"]
    finally:
        shutdown_tracing()


def test_exception_text_never_reaches_span_status(spans: InMemorySpanExporter) -> None:
    """OTel's default status-on-exception would copy raw exception text (e.g. a pydantic
    ``input_value`` holding a bank number) over the masked status (invariant 9)."""

    def failing_call() -> None:
        with llm_span(ModelRole.EXTRACT_T1, HAIKU) as span:
            span.error("validation", "Acct 000123456789 did not parse")
            raise ValueError("input_value='Acct 000123456789'")

    def failing_document() -> None:
        with document_span("extract_and_score"):
            raise KeyError("Routing 021000021 Acct 000123456789")

    with pytest.raises(ValueError):
        failing_call()
    with pytest.raises(KeyError):
        failing_document()
    first, second = spans.get_finished_spans()
    for sp in (first, second):
        assert sp.status.description is not None
        assert "000123456789" not in sp.status.description
        assert "021000021" not in sp.status.description
        assert not sp.events
    assert first.attributes is not None and first.attributes["error.type"] == "validation"
    assert second.status.description == "KeyError"
