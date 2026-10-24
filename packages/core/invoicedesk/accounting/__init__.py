"""Posting adapters (M11). STUB: owned by the `accounting` component."""

from __future__ import annotations

from invoicedesk.config.settings import Settings
from invoicedesk.contracts.accounting import AccountingAdapter


def build_accounting_adapter(settings: Settings) -> AccountingAdapter:
    """QuickBooks Online sandbox adapter in live mode."""
    raise NotImplementedError("accounting component not built yet")
