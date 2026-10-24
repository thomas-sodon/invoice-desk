"""Tracer provider setup: OTLP/HTTP to Langfuse when configured, otherwise nothing."""

from __future__ import annotations

import base64
import threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from invoicedesk.config.settings import Settings

__all__ = ["langfuse_otlp_config", "setup_tracing", "shutdown_tracing"]

_lock = threading.Lock()
_state: dict[str, TracerProvider] = {}


def langfuse_otlp_config(settings: Settings) -> tuple[str, dict[str, str]] | None:
    """``(endpoint, headers)`` for Langfuse's OTLP/HTTP ingest, or ``None`` if not configured.

    Langfuse accepts OTLP traces at ``<host>/api/public/otel/v1/traces`` with HTTP basic auth
    ``public_key:secret_key``.
    """
    if not (
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    ):
        return None
    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key.get_secret_value()}".encode()
    ).decode()
    endpoint = settings.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces"
    return endpoint, {"Authorization": f"Basic {token}"}


def setup_tracing(
    settings: Settings,
    *,
    service_name: str = "invoicedesk",
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install a global tracer provider once. Returns it, or ``None`` when tracing is off.

    ``exporter`` overrides the Langfuse exporter (tests pass an in-memory exporter). Offline
    mode never exports to the network unless an exporter is passed explicitly.
    """
    with _lock:
        if "provider" in _state:
            return _state["provider"]
        if exporter is None:
            cfg = langfuse_otlp_config(settings)
            if cfg is None or settings.mode != "live":
                return None
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            endpoint, headers = cfg
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _state["provider"] = provider
        return provider


def shutdown_tracing() -> None:
    """Flush and stop the exporter (call at process exit)."""
    with _lock:
        provider = _state.pop("provider", None)
        if provider is not None:
            provider.shutdown()
