"""Fake least-privilege accounting adapter with idempotency semantics like QBO ``requestid``."""

from __future__ import annotations

import hashlib
from typing import Literal

from invoicedesk.contracts.accounting import AttachResult, BillRequest, BillResult

__all__ = ["FakeAccounting"]


class FakeAccounting:
    system: Literal["fake"] = "fake"

    def __init__(self) -> None:
        self.bills: dict[str, BillRequest] = {}
        self.attachments: dict[str, tuple[str, str, int]] = {}
        self._by_key: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def create_bill(self, bill: BillRequest, *, idempotency_key: str) -> BillResult:
        self.calls.append(("create_bill", idempotency_key))
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if idempotency_key in self._by_key:
            return BillResult(
                system="fake", external_id=self._by_key[idempotency_key], status="already_exists"
            )
        ext = "bill-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]
        self._by_key[idempotency_key] = ext
        self.bills[ext] = bill
        return BillResult(system="fake", external_id=ext, status="created")

    def attach_file(
        self,
        bill_external_id: str,
        *,
        filename: str,
        content_type: str,
        data: bytes,
        idempotency_key: str,
    ) -> AttachResult:
        self.calls.append(("attach_file", idempotency_key))
        if bill_external_id not in self.bills:
            raise KeyError(bill_external_id)
        if idempotency_key in self._by_key:
            return AttachResult(external_id=self._by_key[idempotency_key], status="already_exists")
        ext = "att-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]
        self._by_key[idempotency_key] = ext
        self.attachments[ext] = (bill_external_id, filename, len(data))
        return AttachResult(external_id=ext, status="created")
