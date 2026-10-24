"""Posting adapters (M11): the least-privilege QuickBooks Online sandbox adapter.

``build_accounting_adapter(settings)`` is the live-mode entry point used by
``invoicedesk.wiring.accounting_adapter``. Offline mode uses ``invoicedesk.fakes.FakeAccounting``.
The demo-only sandbox deleter lives in ``invoicedesk.accounting.sandbox_admin`` and is
deliberately NOT exported here.
"""

from __future__ import annotations

import httpx

from invoicedesk.accounting.base import (
    AccountingAdapter,
    AccountingError,
    AttachResult,
    BillLine,
    BillRequest,
    BillResult,
    idempotency_key_for,
)
from invoicedesk.accounting.oauth import (
    PostgresTokenStore,
    QboTokenManager,
    TokenStore,
    store_key_from_secret,
)
from invoicedesk.accounting.quickbooks import QuickBooksAdapter
from invoicedesk.accounting.transport import (
    SANDBOX_API_BASE,
    QboTransport,
    SandboxOnlyError,
    require_sandbox,
)
from invoicedesk.config.settings import Settings

__all__ = [
    "AccountingAdapter",
    "AccountingError",
    "AttachResult",
    "BillLine",
    "BillRequest",
    "BillResult",
    "QuickBooksAdapter",
    "SandboxOnlyError",
    "build_accounting_adapter",
    "build_qbo_transport",
    "idempotency_key_for",
]


def build_qbo_transport(
    settings: Settings,
    *,
    store: TokenStore | None = None,
    http: httpx.Client | None = None,
    background_refresh: bool = False,
) -> QboTransport:
    """Sandbox transport with a persisted, auto-refreshing token manager."""
    require_sandbox(settings.qbo_environment, SANDBOX_API_BASE)
    missing = [
        name
        for name, val in (
            ("QBO_CLIENT_ID", settings.qbo_client_id),
            ("QBO_CLIENT_SECRET", settings.qbo_client_secret),
            ("QBO_REALM_ID", settings.qbo_realm_id),
            ("QBO_REFRESH_TOKEN", settings.qbo_refresh_token),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"QuickBooks sandbox needs {', '.join(missing)} in .env"
            " (run scripts/qbo_oauth_bootstrap.py once to obtain the refresh token)"
        )
    assert settings.qbo_client_id
    assert settings.qbo_client_secret
    assert settings.qbo_realm_id
    assert settings.qbo_refresh_token
    secret = settings.qbo_client_secret.get_secret_value()
    client = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    tokens = QboTokenManager(
        client_id=settings.qbo_client_id,
        client_secret=secret,
        realm_id=settings.qbo_realm_id,
        seed_refresh_token=settings.qbo_refresh_token.get_secret_value(),
        store=store or PostgresTokenStore(settings.database_url, key=store_key_from_secret(secret)),
        http=client,
    )
    if background_refresh:
        tokens.start_background_refresh()
    return QboTransport(
        realm_id=settings.qbo_realm_id,
        tokens=tokens,
        http=client,
        environment=settings.qbo_environment,
    )


def build_accounting_adapter(
    settings: Settings,
    *,
    store: TokenStore | None = None,
    http: httpx.Client | None = None,
    background_refresh: bool = True,
) -> QuickBooksAdapter:
    """QuickBooks Online sandbox adapter (live mode). Refuses any non-sandbox environment."""
    return QuickBooksAdapter(
        build_qbo_transport(settings, store=store, http=http, background_refresh=background_refresh)
    )
