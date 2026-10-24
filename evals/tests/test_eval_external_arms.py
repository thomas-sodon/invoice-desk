"""Config G (Azure DI), the Langfuse push and the public track, all against fakes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.config.settings import Settings
from invoicedesk.contracts.llm import LLMRequest
from invoicedesk.contracts.preprocess import OcrWord, PageImage, PreprocessedDocument
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.extract import QuarantinedDocument
from invoicedesk.fakes import FakeLLM
from invoicedesk.storage import InMemoryStorage
from invoicedesk.testing.builders import clean_screening
from invoicedesk_evals.azure_invoice import AzureInvoiceExtractor, map_invoice_fields
from invoicedesk_evals.langfuse_push import langfuse_configured, push_results
from invoicedesk_evals.matrix import load_matrix
from invoicedesk_evals.public_track import run_public_source
from PIL import Image
from pydantic import SecretStr

AZURE_DOC = {
    "docType": "invoice",
    "fields": {
        "VendorName": {
            "type": "string",
            "valueString": "Rapid Rooter Plumbing LLC",
            "confidence": 0.97,
            "boundingRegions": [
                {"pageNumber": 1, "polygon": [90, 230, 380, 230, 380, 258, 90, 258]}
            ],
        },
        "InvoiceId": {"type": "string", "valueString": "INV-2231", "confidence": 0.99},
        "InvoiceDate": {"type": "date", "valueDate": "2026-10-10", "confidence": 0.98},
        "PurchaseOrder": {"type": "string", "valueString": "WO-1187", "confidence": 0.9},
        "VendorTaxId": {"type": "string", "content": "00-0004821", "confidence": 0.9},
        "SubTotal": {"type": "currency", "valueCurrency": {"amount": 1150.0}, "confidence": 0.9},
        "TotalTax": {"type": "currency", "valueCurrency": {"amount": 90.0}, "confidence": 0.9},
        "InvoiceTotal": {
            "type": "currency",
            "valueCurrency": {"amount": 1240.10},
            "confidence": 0.95,
        },
        "Items": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        "Description": {
                            "valueString": "Replace kitchen drain trap",
                            "confidence": 0.9,
                        },
                        "Quantity": {"valueNumber": 1, "content": "1", "confidence": 0.9},
                        "UnitPrice": {"valueCurrency": {"amount": 850.0}, "confidence": 0.9},
                        "Amount": {"valueCurrency": {"amount": 850.0}, "confidence": 0.9},
                    },
                }
            ],
        },
    },
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (1237, 1600), 255).save(buf, format="PNG")
    return buf.getvalue()


def _doc(storage: InMemoryStorage) -> PreprocessedDocument:
    storage.put("t/renders/d1/page-0.png", _png(), content_type="image/png")
    return PreprocessedDocument(
        document_id="d1",
        sha256="0" * 64,
        mime="image/png",
        is_native_pdf=False,
        pages=[
            PageImage(
                index=0,
                width=1237,
                height=1600,
                storage_key="t/renders/d1/page-0.png",
                sha256="1" * 64,
                scale=1.0,
            )
        ],
        words=[OcrWord(id=0, page=0, text="x", box=(1, 1, 5, 5), conf=0.9)],
        enhanced_words=[],
        text_layer=[],
        phash="0" * 16,
        ocr_engine="stub",
    )


def test_azure_mapping_uses_integer_cents() -> None:
    m = map_invoice_fields(json.loads(json.dumps(AZURE_DOC)))
    assert m["total"]["value_cents"] == 124010
    assert m["subtotal"]["value_cents"] == 115000
    assert m["vendor_tax_id_last4"]["value"] == "4821"
    assert m["vendor_name"]["source"]["fallback_box"] == [90, 230, 380, 258]
    assert m["line_items"][0]["amount"]["value_cents"] == 85000


def test_azure_extractor_against_mock_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.method == "POST":
            assert req.headers["Ocp-Apim-Subscription-Key"] == "k"
            assert "prebuilt-invoice:analyze" in str(req.url)
            return httpx.Response(202, headers={"Operation-Location": "https://az.test/op/1"})
        if len([c for c in calls if c.method == "GET"]) == 1:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(
            200, json={"status": "succeeded", "analyzeResult": {"documents": [AZURE_DOC]}}
        )

    storage = InMemoryStorage()
    ex = AzureInvoiceExtractor(
        storage,
        endpoint="https://az.test",
        key="k",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_s=0,
    )
    out = ex.extract(_doc(storage), screening=clean_screening())
    assert out.final.total.value_cents == 124010
    assert out.final.invoice_number.value == "INV-2231"
    assert out.tiers == []
    names = {f.name for f in out.fields}
    assert {"total", "vendor_name"} <= names
    assert any(n.startswith("line_items") for n in names)
    assert ex.cost_for("d1") > 0 and ex.cost_for("d1") == 0
    with pytest.raises(QuarantinedDocument):
        ex.extract(_doc(storage), screening=ScreeningOutcome(results=[], quarantined=True))


def test_azure_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_DI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_DI_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AzureInvoiceExtractor(InMemoryStorage())
    assert load_matrix().get("G").missing_env() == ["AZURE_DI_ENDPOINT", "AZURE_DI_KEY"]


def test_langfuse_push(make_score: Any) -> None:
    unconfigured = Settings()  # pyright: ignore[reportCallIssue]
    unconfigured = unconfigured.model_copy(update={"langfuse_host": None})
    assert not langfuse_configured(unconfigured)
    assert push_results(unconfigured, run_id="r", config="D", split="dev", scores=[]) == {
        "skipped": 1
    }
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"].startswith("Basic ")
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={})

    s = unconfigured.model_copy(
        update={
            "langfuse_host": "https://lf.test",
            "langfuse_public_key": "pk",
            "langfuse_secret_key": SecretStr("sk"),
        }
    )
    scores = [make_score(example_id="a", rules_failed={"R6": "flag"}), make_score(example_id="b")]
    res = push_results(
        s,
        run_id="0123456789",
        config="D",
        split="dev",
        scores=scores,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert res == {"items": 2, "traces": 2}
    paths = [p for p, _ in seen]
    assert paths[0] == "/api/public/datasets"
    assert paths.count("/api/public/dataset-items") == 2
    assert paths.count("/api/public/dataset-run-items") == 2
    batch = next(b for p, b in seen if p == "/api/public/ingestion")["batch"]
    assert {e["type"] for e in batch} == {"trace-create", "score-create"}


def test_public_track_skips_without_cache(tmp_path: Path) -> None:
    spec = load_matrix().get("D")
    out = run_public_source("sroie", spec, FakeLLM(), cache=tmp_path)
    assert "skipped" in out


class _StubPre:
    def __init__(self, storage: InMemoryStorage) -> None:
        self.storage = storage

    def preprocess(
        self, data: bytes, *, mime: str, document_id: str, filename: str | None = None
    ) -> PreprocessedDocument:
        doc = _doc(self.storage)
        return doc.model_copy(update={"document_id": document_id})


def test_public_track_scores_mapped_fields(tmp_path: Path) -> None:
    cache = tmp_path / "sroie"
    (cache / "data").mkdir(parents=True)
    for i in range(2):
        (cache / "data" / f"r{i}.jpg").write_bytes(b"jpeg-bytes")
    rows = [
        {
            "source": "sroie",
            "id": f"r{i}",
            "image": f"data/r{i}.jpg",
            "vendor_name": "KEDAI FICTIONAL SDN BHD",
            "invoice_date": "2018-03-01",
            "total_cents": 900 + i,
            "currency": "MYR",
            "vendor_address": None,
        }
        for i in range(2)
    ]
    (cache / "index.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    from invoicedesk.testing.builders import date, make_extraction, money, text

    answer = make_extraction(
        vendor_name=text("Kedai Fictional Sdn. Bhd."),
        invoice_date=date("2018-03-01"),
        total=money(900),
        vendor_address=text(None),
    ).model_dump(mode="json")

    def respond(req: LLMRequest) -> dict[str, Any]:
        if req.role == ModelRole.INJECTION_CLASSIFIER:
            return {"verdict": "clean", "categories": ["none"], "spans": []}
        return answer

    storage = InMemoryStorage()
    out = run_public_source(
        "sroie",
        load_matrix().get("C"),
        FakeLLM(respond),
        cache=tmp_path,
        preprocessor=_StubPre(storage),
        storage=storage,
        n_resamples=50,
    )
    assert out["n_docs"] == 2, out
    assert out["status_counts"] == {"extracted": 2}, out["docs"]
    # r0: vendor, date, total right (3 TP). r1: total wrong (FP+FN), vendor+date right.
    assert out["per_field"]["total"]["recall"] == 0.5
    assert out["per_field"]["vendor_name"]["f1"] == 1.0
    assert out["field_f1"]["value"] == pytest.approx(2 * 5 / (2 * 5 + 1 + 1))
