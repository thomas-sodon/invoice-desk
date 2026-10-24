"""DEMO UTILITY, NOT A POSTING ADAPTER: delete InvoiceDesk demo bills from the QBO sandbox.

``make demo-reset`` (``scripts/demo_reset.py``) uses this to clear the sandbox between demo
recordings. It is deliberately separate from :class:`invoicedesk.accounting.QuickBooksAdapter`:

* it is not an ``AccountingAdapter``, is not exported from ``invoicedesk.accounting`` and is never
  wired into the pipeline, the API, the worker or the MCP server;
* it runs only against the sandbox (the shared transport refuses anything else);
* it only deletes Bills whose ``PrivateNote`` carries the InvoiceDesk marker, so it cannot touch
  bills someone keyed by hand into the same sandbox company.

QBO deletes a Bill with ``POST /bill?operation=delete`` and a body of ``{"Id", "SyncToken"}`` (the
API's form of DELETE; the SyncToken is read first with ``GET /bill/{id}``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from invoicedesk.accounting.quickbooks import MARKER_PREFIX
from invoicedesk.accounting.transport import QboApiError, QboTransport, encode_json

__all__ = ["DeleteOutcome", "QboSandboxAdmin", "SandboxBill"]

log = logging.getLogger("invoicedesk.accounting.sandbox_admin")

_ID_RE = re.compile(r"^[0-9]{1,20}$")
_NOT_FOUND = {"610", "5010"}  # Object Not Found / stale object (already deleted)
_PAGE = 1000


@dataclass(frozen=True, slots=True)
class SandboxBill:
    id: str
    sync_token: str
    doc_number: str
    private_note: str


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    bill_id: str
    result: Literal["deleted", "missing", "refused"]
    detail: str = ""


def _bill(b: Mapping[str, Any]) -> SandboxBill:
    return SandboxBill(
        id=str(b.get("Id", "")),
        sync_token=str(b.get("SyncToken", "0")),
        doc_number=str(b.get("DocNumber", "")),
        private_note=str(b.get("PrivateNote", "")),
    )


class QboSandboxAdmin:
    def __init__(self, transport: QboTransport, *, request_id_prefix: str = "demo-reset") -> None:
        self._t = transport
        self._prefix = request_id_prefix

    def get_bill(self, bill_id: str) -> SandboxBill | None:
        if not _ID_RE.match(bill_id):
            raise ValueError(f"invalid bill id {bill_id!r}")
        try:
            resp = self._t.request("GET", f"bill/{bill_id}")
        except QboApiError as exc:
            if set(exc.codes) & _NOT_FOUND or exc.status_code == 404:
                return None
            raise
        b = resp.body.get("Bill") if isinstance(resp.body, Mapping) else None
        return _bill(b) if isinstance(b, Mapping) else None

    def list_invoicedesk_bills(self) -> list[SandboxBill]:
        """Every sandbox Bill carrying the InvoiceDesk marker (paged query, filtered here because
        QBO's query language cannot filter on PrivateNote)."""
        out: list[SandboxBill] = []
        start = 1
        while True:
            cols = "Id, SyncToken, DocNumber, PrivateNote"
            page = f"startposition {start} maxresults {_PAGE}"
            query = f"select {cols} from Bill {page}"  # noqa: S608 (QBO query, ints only)
            resp = self._t.request("GET", "query", params={"query": query})
            qr = resp.body.get("QueryResponse", {}) if isinstance(resp.body, Mapping) else {}
            rows = qr.get("Bill", []) or []
            out.extend(_bill(b) for b in rows if MARKER_PREFIX in str(b.get("PrivateNote", "")))
            if len(rows) < _PAGE:
                return out
            start += _PAGE

    def delete_bill(self, bill_id: str) -> DeleteOutcome:
        bill = self.get_bill(bill_id)
        if bill is None:
            return DeleteOutcome(bill_id, "missing", "not in sandbox (already deleted?)")
        if MARKER_PREFIX not in bill.private_note:
            return DeleteOutcome(bill_id, "refused", "bill was not posted by InvoiceDesk")
        try:
            self._t.request(
                "POST",
                "bill",
                params={"operation": "delete"},
                json_text=encode_json({"Id": bill.id, "SyncToken": bill.sync_token}),
                request_id=f"{self._prefix}-{bill.id}-{bill.sync_token}",
            )
        except QboApiError as exc:
            if set(exc.codes) & _NOT_FOUND:
                return DeleteOutcome(bill_id, "missing", "deleted concurrently")
            raise
        log.info("demo reset: deleted sandbox bill %s (DocNumber %s)", bill.id, bill.doc_number)
        return DeleteOutcome(bill_id, "deleted")
