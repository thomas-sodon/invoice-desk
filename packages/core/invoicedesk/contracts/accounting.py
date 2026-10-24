"""Least-privilege posting adapter contract (M11; CLAUDE.md invariant 4).

The adapter can ONLY create a Bill and attach a file. Request models forbid extra fields, so
there is no way to pass bank/remit details, vendor edits or payments through it. Every call
carries an idempotency key (QBO ``requestid``).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.money import Cents

__all__ = [
    "AccountingAdapter",
    "AccountingError",
    "AttachResult",
    "BillLine",
    "BillRequest",
    "BillResult",
    "idempotency_key_for",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BillLine(_M):
    description: str = Field(max_length=4000)
    amount_cents: Cents
    expense_account_ref: str  # QBO Account Id from tenant mapping


class BillRequest(_M):
    vendor_ref: str  # external vendor id in the accounting system (from the vendor master)
    doc_number: str = Field(max_length=21)  # QBO DocNumber limit
    txn_date: dt.date
    due_date: dt.date | None = None
    lines: tuple[BillLine, ...]
    total_cents: Cents
    memo: str | None = Field(None, max_length=4000)  # e.g. "WO-1187 Unit 12B"


class BillResult(_M):
    system: Literal["qbo", "xero", "fake"]
    external_id: str
    status: Literal["created", "already_exists"]
    raw_sync_token: str | None = None


class AttachResult(_M):
    external_id: str
    status: Literal["created", "already_exists"]


class AccountingError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def idempotency_key_for(document_id: str, action: Literal["bill", "attach"]) -> str:
    """Deterministic key so a retry of the same document can never double-post."""
    return f"invoicedesk-{action}-{document_id}"


@runtime_checkable
class AccountingAdapter(Protocol):
    @property
    def system(self) -> Literal["qbo", "xero", "fake"]: ...

    def create_bill(self, bill: BillRequest, *, idempotency_key: str) -> BillResult: ...

    def attach_file(
        self,
        bill_external_id: str,
        *,
        filename: str,
        content_type: str,
        data: bytes,
        idempotency_key: str,
    ) -> AttachResult: ...
