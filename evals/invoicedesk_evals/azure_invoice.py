"""Config G: Azure AI Document Intelligence ``prebuilt-invoice`` alone (commercial baseline,
DESIGN §5.6). Optional: it runs only with ``INVOICEDESK_MODE=live`` plus ``AZURE_DI_ENDPOINT``
and ``AZURE_DI_KEY``; the runner skips G otherwise. Tests drive it through an
``httpx.MockTransport``.

REST flow (API version 2024-11-30): ``POST {endpoint}/documentintelligence/documentModels/
prebuilt-invoice:analyze?api-version=...`` with ``{"base64Source": ...}`` returns ``202`` and an
``Operation-Location`` to poll until ``status`` is ``succeeded``. Each processed page PNG (the
same 1,600 px renders Claude sees) is analyzed, so polygons are already in processed-image
pixels. Money is parsed with ``Decimal`` (never float) and converted to integer cents.

It plugs into the real pipeline as an :class:`~invoicedesk.contracts.extractor.Extractor`, so
screening, the rules engine and routing are unchanged; only the reader differs. It is
tool-less like every reader, and refuses quarantined documents (invariant 8).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

import httpx
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.extractor import ExtractionOutcome
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import PreprocessedDocument
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.extract import QuarantinedDocument, field_results

__all__ = ["API_VERSION", "USD_PER_PAGE", "AzureInvoiceExtractor", "map_invoice_fields"]

API_VERSION = "2024-11-30"
#: prebuilt-invoice list price, $10 per 1,000 pages (free tier: 500 pages/month).
USD_PER_PAGE = Decimal("0.01")


def _cents(field: dict[str, Any] | None) -> int | None:
    if not field:
        return None
    cur = field.get("valueCurrency")
    amount = cur.get("amount") if isinstance(cur, dict) else field.get("valueNumber")
    if amount is None:
        return None
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _box(field: dict[str, Any] | None, page_offset: int) -> tuple[list[int] | None, int]:
    if not field:
        return None, 0
    regions = field.get("boundingRegions") or []
    if not regions:
        return None, 0
    r = regions[0]
    poly = [float(v) for v in r.get("polygon") or []]
    if len(poly) < 8:
        return None, page_offset
    xs, ys = poly[0::2], poly[1::2]
    return [int(min(xs)), int(min(ys)), int(max(xs) + 0.999), int(max(ys) + 0.999)], page_offset


def _text(field: dict[str, Any] | None) -> str | None:
    if not field:
        return None
    for k in ("valueString", "content"):
        v = field.get(k)
        if v:
            return str(v)
    addr = field.get("valueAddress")
    if isinstance(addr, dict):
        return ", ".join(str(v) for v in addr.values() if v) or None
    return None


def _conf(field: dict[str, Any] | None) -> float:
    if not field:
        return 0.0
    return float(min(1.0, max(0.0, float(field.get("confidence") or 0.0))))


def map_invoice_fields(document: dict[str, Any], *, page_offset: int = 0) -> dict[str, Any]:
    """Map one ``analyzeResult.documents[i]`` to ``InvoiceExtraction`` JSON."""
    f: dict[str, Any] = document.get("fields") or {}

    def ref(field: dict[str, Any] | None) -> dict[str, Any]:
        box, _ = _box(field, page_offset)
        return {"word_ids": [], "fallback_box": box}

    def text(name: str, *, value: str | None = None) -> dict[str, Any]:
        fld = f.get(name)
        return {
            "value": value if value is not None else _text(fld),
            "confidence": _conf(fld),
            "source": ref(fld),
        }

    def money(name: str) -> dict[str, Any]:
        fld = f.get(name)
        return {
            "value_cents": _cents(fld),
            "currency": "USD",
            "confidence": _conf(fld),
            "source": ref(fld),
        }

    def date(name: str) -> dict[str, Any]:
        fld = f.get(name)
        return {
            "value_iso": (fld or {}).get("valueDate"),
            "confidence": _conf(fld),
            "source": ref(fld),
        }

    tax_id = _text(f.get("VendorTaxId"))
    digits = "".join(c for c in tax_id or "" if c.isdigit())
    items: list[dict[str, Any]] = []
    for it in (f.get("Items") or {}).get("valueArray") or []:
        o = it.get("valueObject") or {}
        qty = o.get("Quantity")
        q = (
            None
            if not qty
            else (
                qty.get("content")
                or (None if qty.get("valueNumber") is None else str(qty.get("valueNumber")))
            )
        )
        items.append(
            {
                "description": {
                    "value": _text(o.get("Description")),
                    "confidence": _conf(o.get("Description")),
                    "source": ref(o.get("Description")),
                },
                "quantity": {"value": q, "confidence": _conf(qty), "source": ref(qty)},
                "unit_price": {
                    "value_cents": _cents(o.get("UnitPrice")),
                    "currency": "USD",
                    "confidence": _conf(o.get("UnitPrice")),
                    "source": ref(o.get("UnitPrice")),
                },
                "amount": {
                    "value_cents": _cents(o.get("Amount")),
                    "currency": "USD",
                    "confidence": _conf(o.get("Amount")),
                    "source": ref(o.get("Amount")),
                },
            }
        )
    return {
        "doc_type": "invoice",
        "vendor_name": text("VendorName"),
        "vendor_address": text("VendorAddress"),
        "vendor_tax_id_last4": text("VendorTaxId", value=digits[-4:] or None),
        "invoice_number": text("InvoiceId"),
        "invoice_date": date("InvoiceDate"),
        "due_date": date("DueDate"),
        "work_order_number": text("PurchaseOrder"),
        "property_or_unit": text("ServiceAddress"),
        "line_items": items,
        "subtotal": money("SubTotal"),
        "tax": money("TotalTax"),
        "total": money("InvoiceTotal"),
        "remit_to_bank_details": text("RemittanceAddress"),
        "handwriting_present": False,
        "legibility_issues": [],
        "embedded_instructions": [],
    }


class AzureInvoiceExtractor:
    """``Extractor`` backed by Azure DI prebuilt-invoice (no LLM tiers)."""

    def __init__(
        self,
        storage: Storage,
        *,
        endpoint: str | None = None,
        key: str | None = None,
        client: httpx.Client | None = None,
        poll_interval_s: float = 1.0,
        timeout_s: float = 120.0,
    ) -> None:
        self.storage = storage
        self.endpoint = (endpoint or os.environ.get("AZURE_DI_ENDPOINT", "")).rstrip("/")
        self.key = key or os.environ.get("AZURE_DI_KEY", "")
        if not self.endpoint or not self.key:
            raise RuntimeError("AZURE_DI_ENDPOINT and AZURE_DI_KEY must be set for config G")
        self.client = client or httpx.Client(timeout=30.0)
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cost: dict[str, Decimal] = {}

    def cost_for(self, document_id: str) -> Decimal:
        with self._lock:
            return self._cost.pop(document_id, Decimal(0))

    def _analyze(self, png: bytes) -> dict[str, Any]:
        url = (
            f"{self.endpoint}/documentintelligence/documentModels/prebuilt-invoice:analyze"
            f"?api-version={API_VERSION}"
        )
        headers = {"Ocp-Apim-Subscription-Key": self.key}
        r = self.client.post(
            url, headers=headers, json={"base64Source": base64.b64encode(png).decode()}
        )
        if r.status_code != 202:
            raise RuntimeError(f"Azure DI analyze returned HTTP {r.status_code}")
        op = r.headers.get("Operation-Location")
        if not op:
            raise RuntimeError("Azure DI analyze returned no Operation-Location")
        deadline = time.monotonic() + self.timeout_s
        while True:
            pr = self.client.get(op, headers=headers)
            body = json.loads(pr.content, parse_float=Decimal)
            status = body.get("status")
            if status == "succeeded":
                return body.get("analyzeResult") or {}
            if status == "failed":
                raise RuntimeError("Azure DI analysis failed")
            if time.monotonic() > deadline:
                raise TimeoutError("Azure DI analysis timed out")
            time.sleep(self.poll_interval_s)

    def extract(
        self,
        doc: PreprocessedDocument,
        *,
        screening: ScreeningOutcome,
        email_body: str | None = None,
        max_tier: Literal["haiku", "sonnet", "opus"] = "opus",
    ) -> ExtractionOutcome:
        if screening.quarantined:
            raise QuarantinedDocument(
                f"document {doc.document_id} is quarantined by screening; extraction refused"
            )
        merged: dict[str, Any] | None = None
        pages = sorted(doc.pages, key=lambda p: p.index)
        for p in pages:
            result = self._analyze(self.storage.get(p.storage_key))
            docs = result.get("documents") or []
            if not docs:
                continue
            mapped = map_invoice_fields(docs[0], page_offset=p.index)
            if merged is None:
                merged = mapped
            else:  # later pages only fill gaps (totals often sit on the last page)
                for k, v in mapped.items():
                    if isinstance(v, dict) and any(
                        v.get(x) is not None for x in ("value", "value_cents", "value_iso")
                    ):
                        cur = merged.get(k) or {}
                        if all(cur.get(x) is None for x in ("value", "value_cents", "value_iso")):
                            merged[k] = v
                merged["line_items"] = merged["line_items"] + mapped["line_items"]
        cost = USD_PER_PAGE * len(pages)
        with self._lock:
            self._cost[doc.document_id] = self._cost.get(doc.document_id, Decimal(0)) + cost
        if merged is None:
            from invoicedesk.pipeline.graph import empty_extraction

            final = empty_extraction("Azure DI found no invoice on any page")
        else:
            final = InvoiceExtraction.model_validate(_jsonable(merged))
        calibrated = {
            name: getattr(final, name).confidence
            for name in _SCORED
            if hasattr(getattr(final, name), "confidence")
        }
        return ExtractionOutcome(
            final=final,
            fields=field_results(final, calibrated, {}, doc_type_conf=1.0),
            tiers=[],
            escalated_to="haiku",
            total_cost_usd=str(cost),
        )


_SCORED = (
    "vendor_name",
    "vendor_address",
    "vendor_tax_id_last4",
    "invoice_number",
    "invoice_date",
    "due_date",
    "work_order_number",
    "property_or_unit",
    "subtotal",
    "tax",
    "total",
    "remit_to_bank_details",
)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)  # confidences only; money was converted to int cents already
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v
