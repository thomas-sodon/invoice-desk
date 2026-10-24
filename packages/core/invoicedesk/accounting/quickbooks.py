"""Least-privilege QuickBooks Online posting adapter (M11; CLAUDE.md invariant 4; §4.1, §5.5).

:class:`QuickBooksAdapter` has exactly two operations:

* :meth:`~QuickBooksAdapter.create_bill`:
  ``POST /v3/company/{realmId}/bill?requestid=<key>&minorversion=75`` with ``VendorRef``,
  ``TxnDate``, ``DueDate``, ``DocNumber``, ``PrivateNote`` (the memo) and one
  ``AccountBasedExpenseLineDetail`` line per :class:`BillLine`. Cents become exact decimal
  literals via :class:`~decimal.Decimal`, never a float.
* :meth:`~QuickBooksAdapter.attach_file`: multipart ``POST /v3/company/{realmId}/upload`` with an
  ``Attachable`` whose ``AttachableRef`` points at the Bill, under its own ``requestid``.

It has no BillPayment, vendor, bank, update or delete operation, and its transport is private.
Deleting demo bills lives in :mod:`invoicedesk.accounting.sandbox_admin`, a demo-only utility that
the pipeline and MCP server never receive.

**Idempotency.** QBO answers a repeated ``requestid`` with the original response instead of
acting again, so a retried post returns the same Bill. The adapter reports ``already_exists``
when (a) this adapter instance already posted that key, or (b) the returned Bill was created
before this call started (minus a clock-skew allowance), i.e. a replay after a crash. If QBO's
requestid cache has expired and the duplicate-DocNumber check fires (fault 6140), the adapter
looks the Bill up by ``DocNumber`` and accepts it only when its ``PrivateNote`` carries this
idempotency key; any other match is a genuine duplicate and is not retryable.

QBO limits ``requestid`` to 50 characters; longer keys (``idempotency_key_for`` yields 53) map to
a deterministic UUIDv5, so a retry of the same key always sends the same ``requestid``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Literal

from invoicedesk.accounting.transport import (
    QboApiError,
    QboFault,
    QboTransport,
    cents_to_decimal,
    encode_json,
)
from invoicedesk.contracts.accounting import (
    AccountingError,
    AttachResult,
    BillRequest,
    BillResult,
)
from invoicedesk.masking import mask_text

__all__ = [
    "ALLOWED_ATTACHMENT_TYPES",
    "MARKER_PREFIX",
    "QboTotalMismatch",
    "QuickBooksAdapter",
    "marker_for",
    "request_id_for",
]

log = logging.getLogger("invoicedesk.accounting.qbo")

#: Text stamped into every Bill's PrivateNote; identifies InvoiceDesk bills and their key.
MARKER_PREFIX = "InvoiceDesk ref "
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_REQUEST_ID_NS = uuid.UUID("6b0f3c2e-6a43-4d1e-9a51-0c2b6f8a7d10")
_ENTITY_ID_RE = re.compile(r"^[0-9]{1,20}$")
_DUPLICATE_DOCNUMBER = "6140"
#: QBO attachment types InvoiceDesk sends (originals are PDF or images).
ALLOWED_ATTACHMENT_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/tiff", "text/plain"}
)
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # QBO's per-file limit
PRIVATE_NOTE_MAX = 4000


def request_id_for(idempotency_key: str) -> str:
    """The QBO ``requestid`` for an idempotency key (deterministic)."""
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    if _REQUEST_ID_RE.match(idempotency_key):
        return idempotency_key
    return uuid.uuid5(_REQUEST_ID_NS, idempotency_key).hex


def marker_for(idempotency_key: str) -> str:
    return f"{MARKER_PREFIX}{idempotency_key}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class QboTotalMismatch(AccountingError):
    """QBO computed a different total than we sent (e.g. a vendor default tax was applied)."""

    def __init__(self, message: str, *, external_id: str) -> None:
        super().__init__(message, retryable=False)
        self.external_id = external_id


def _safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .") or "invoice"
    if len(base) > 100:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 100 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:100]
    return base


def _created_at(entity: Mapping[str, Any]) -> dt.datetime | None:
    meta = entity.get("MetaData")
    if not isinstance(meta, Mapping):
        return None
    raw = meta.get("CreateTime")
    if not isinstance(raw, str):
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)


class QuickBooksAdapter:
    """``AccountingAdapter`` for the QBO sandbox. Public surface: ``system``, ``create_bill``,
    ``attach_file``. Nothing else."""

    system: Literal["qbo"] = "qbo"

    def __init__(
        self,
        transport: QboTransport,
        *,
        clock: Callable[[], dt.datetime] = _utcnow,
        replay_skew: dt.timedelta = dt.timedelta(seconds=60),
    ) -> None:
        self.__transport = transport
        self.__clock = clock
        self.__skew = replay_skew
        self.__seen: dict[str, str] = {}

    # ------------------------------------------------------------------ create_bill
    def create_bill(self, bill: BillRequest, *, idempotency_key: str) -> BillResult:
        rid = request_id_for(idempotency_key)
        if not bill.lines:
            raise AccountingError("a bill needs at least one line", retryable=False)
        line_sum = sum(line.amount_cents for line in bill.lines)
        if line_sum != bill.total_cents:
            raise AccountingError(
                f"bill lines sum to {line_sum} cents but total is {bill.total_cents} cents",
                retryable=False,
            )
        payload = self.__bill_payload(bill, idempotency_key)
        started = self.__clock()
        try:
            resp = self.__transport.request(
                "POST", "bill", json_text=encode_json(payload), request_id=rid
            )
        except QboApiError as exc:
            if _DUPLICATE_DOCNUMBER in exc.codes:
                return self.__resolve_duplicate(bill, idempotency_key, exc)
            raise
        entity = resp.body.get("Bill") if isinstance(resp.body, Mapping) else None
        if not isinstance(entity, Mapping) or not entity.get("Id"):
            raise AccountingError("QBO bill response has no Bill.Id", retryable=False)
        ext = str(entity["Id"])
        status = self.__status(idempotency_key, ext, entity, started)
        total = entity.get("TotalAmt")
        if total is not None and Decimal(str(total)) != cents_to_decimal(bill.total_cents):
            raise QboTotalMismatch(
                f"QBO bill {ext} total {total} != expected {cents_to_decimal(bill.total_cents)}",
                external_id=ext,
            )
        log.info("qbo bill %s %s (requestid=%s)", ext, status, rid)
        return BillResult(
            system="qbo",
            external_id=ext,
            status=status,
            raw_sync_token=str(entity.get("SyncToken"))
            if entity.get("SyncToken") is not None
            else None,
        )

    def __bill_payload(self, bill: BillRequest, idempotency_key: str) -> dict[str, Any]:
        marker = marker_for(idempotency_key)
        memo = mask_text(bill.memo) if bill.memo else ""
        note = f"{memo} | {marker}" if memo else marker
        if len(note) > PRIVATE_NOTE_MAX:
            note = note[: PRIVATE_NOTE_MAX - len(marker) - 4] + " | " + marker
        payload: dict[str, Any] = {
            "VendorRef": {"value": bill.vendor_ref},
            "TxnDate": bill.txn_date.isoformat(),
            "DocNumber": bill.doc_number,
            "PrivateNote": note,
            "Line": [
                {
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Amount": cents_to_decimal(line.amount_cents),
                    "Description": mask_text(line.description) or "",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": line.expense_account_ref}
                    },
                }
                for line in bill.lines
            ],
        }
        if bill.due_date is not None:
            payload["DueDate"] = bill.due_date.isoformat()
        return payload

    def __resolve_duplicate(
        self, bill: BillRequest, idempotency_key: str, exc: QboApiError
    ) -> BillResult:
        """6140 = DocNumber already used. Ours (marker matches) -> already_exists; else error."""
        doc = bill.doc_number.replace("\\", "\\\\").replace("'", "\\'")
        # QBO query language, not SQL; the string literal is escaped above.
        cols = "Id, SyncToken, DocNumber, VendorRef, PrivateNote"
        query = f"select {cols} from Bill where DocNumber = '{doc}'"  # noqa: S608
        resp = self.__transport.request("GET", "query", params={"query": query})
        qr = resp.body.get("QueryResponse", {}) if isinstance(resp.body, Mapping) else {}
        marker = marker_for(idempotency_key)
        for b in qr.get("Bill", []) or []:
            vref = (b.get("VendorRef") or {}).get("value")
            if marker in str(b.get("PrivateNote", "")) and str(vref) == bill.vendor_ref:
                ext = str(b["Id"])
                self.__seen[idempotency_key] = ext
                log.info("qbo bill %s already_exists (found by DocNumber + marker)", ext)
                return BillResult(
                    system="qbo",
                    external_id=ext,
                    status="already_exists",
                    raw_sync_token=str(b.get("SyncToken"))
                    if b.get("SyncToken") is not None
                    else None,
                )
        raise QboApiError(
            f"QBO already has a bill numbered {bill.doc_number!r} that InvoiceDesk did not post"
            " (possible duplicate invoice)",
            retryable=False,
            status_code=exc.status_code,
            faults=exc.faults,
            intuit_tid=exc.intuit_tid,
        ) from exc

    def __status(
        self,
        idempotency_key: str,
        external_id: str,
        entity: Mapping[str, Any],
        started: dt.datetime,
    ) -> Literal["created", "already_exists"]:
        seen = self.__seen.get(idempotency_key)
        self.__seen[idempotency_key] = external_id
        if seen == external_id:
            return "already_exists"
        created = _created_at(entity)
        if created is not None and created < started - self.__skew:
            return "already_exists"
        return "created"

    # ------------------------------------------------------------------ attach_file
    def attach_file(
        self,
        bill_external_id: str,
        *,
        filename: str,
        content_type: str,
        data: bytes,
        idempotency_key: str,
    ) -> AttachResult:
        rid = request_id_for(idempotency_key)
        if not _ENTITY_ID_RE.match(bill_external_id or ""):
            raise AccountingError(f"invalid QBO bill id {bill_external_id!r}", retryable=False)
        ctype = content_type.split(";", 1)[0].strip().lower()
        if ctype not in ALLOWED_ATTACHMENT_TYPES:
            raise AccountingError(f"attachment type {ctype!r} not allowed", retryable=False)
        if not data:
            raise AccountingError("attachment is empty", retryable=False)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AccountingError("attachment exceeds QBO's 100 MB limit", retryable=False)
        name = _safe_filename(filename)
        metadata = {
            "AttachableRef": [
                {"EntityRef": {"type": "Bill", "value": bill_external_id}, "IncludeOnSend": False}
            ],
            "FileName": name,
            "ContentType": ctype,
            "Note": marker_for(idempotency_key),
        }
        files = [
            (
                "file_metadata_01",
                ("attachment.json", encode_json(metadata).encode(), "application/json"),
            ),
            ("file_content_01", (name, data, ctype)),
        ]
        started = self.__clock()
        resp = self.__transport.request("POST", "upload", files=files, request_id=rid)
        items = resp.body.get("AttachableResponse") if isinstance(resp.body, Mapping) else None
        if not isinstance(items, list) or not items:
            raise AccountingError("QBO upload response has no AttachableResponse", retryable=False)
        first = items[0]
        faults = QboFault.list_from(first)
        if faults:
            raise QboApiError(
                "QBO rejected the attachment: "
                + "; ".join(f"[{f.code}] {f.message}" for f in faults),
                retryable=False,
                status_code=resp.status_code,
                faults=faults,
                intuit_tid=resp.intuit_tid,
            )
        att = first.get("Attachable") if isinstance(first, Mapping) else None
        if not isinstance(att, Mapping) or not att.get("Id"):
            raise AccountingError("QBO upload response has no Attachable.Id", retryable=False)
        ext = str(att["Id"])
        status = self.__status(idempotency_key, ext, att, started)
        log.info(
            "qbo attachable %s for bill %s %s (requestid=%s)", ext, bill_external_id, status, rid
        )
        return AttachResult(external_id=ext, status=status)
