"""Composition root: picks real vs fake implementations from Settings.

Offline mode (default, tests, demo without keys) never touches a paid or external service:
LLM -> ReplayLLM over ``data/replay`` (falls back to FakeLLM only when a component supplies one),
accounting -> FakeAccounting, events -> InMemoryEventPublisher, storage -> LocalStorage.
Live mode lazily imports the real implementations from their component modules.
"""

from __future__ import annotations

import importlib
from typing import Any

from invoicedesk.config.settings import Settings, get_settings
from invoicedesk.contracts.accounting import AccountingAdapter
from invoicedesk.contracts.infra import EventPublisher, Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.fakes.accounting import FakeAccounting
from invoicedesk.fakes.events import InMemoryEventPublisher
from invoicedesk.fakes.llm import ReplayLLM
from invoicedesk.storage import InMemoryStorage, LocalStorage

__all__ = ["accounting_adapter", "event_publisher", "llm_client", "storage"]


def _load(path: str) -> Any:
    mod, _, attr = path.partition(":")
    return getattr(importlib.import_module(mod), attr)


def llm_client(settings: Settings | None = None, *, fallback: LLMClient | None = None) -> LLMClient:
    s = settings or get_settings()
    if s.mode == "live":
        if s.anthropic_api_key is None:
            raise RuntimeError("INVOICEDESK_MODE=live requires ANTHROPIC_API_KEY")
        cls = _load("invoicedesk.llm.anthropic_client:AnthropicLLMClient")
        return cls(api_key=s.anthropic_api_key.get_secret_value())
    return ReplayLLM(s.replay_dir, fallback=fallback)


def storage(settings: Settings | None = None) -> Storage:
    s = settings or get_settings()
    if s.storage_backend == "memory":
        return InMemoryStorage()
    if s.storage_backend == "supabase":
        return _load("invoicedesk_api.storage_supabase:SupabaseStorage").from_settings(s)
    return LocalStorage(
        s.storage_local_dir,
        signing_secret=s.session_signing_secret.get_secret_value(),
        base_url=s.api_base_url,
    )


def accounting_adapter(settings: Settings | None = None) -> AccountingAdapter:
    s = settings or get_settings()
    if s.mode == "live" and s.qbo_client_id:
        return _load("invoicedesk.accounting:build_accounting_adapter")(s)
    return FakeAccounting()


def event_publisher(settings: Settings | None = None) -> EventPublisher:
    s = settings or get_settings()
    if s.mode == "live":
        return _load("invoicedesk.pipeline.events:PgNotifyPublisher").from_settings(s)
    return InMemoryEventPublisher()
