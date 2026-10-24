import json
from decimal import Decimal

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.accounting import BillLine, BillRequest, idempotency_key_for
from invoicedesk.contracts.extraction import (
    HEADER_FIELDS,
    InvoiceExtraction,
    field_value,
    iter_header_fields,
    parse_iso_date,
)
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.contracts.llm import LLMRequest, SystemBlock, TextBlock
from invoicedesk.contracts.screening import (
    InjectionVerdict,
    ScreeningOutcome,
    ScreenResult,
    ScreenSpan,
    should_quarantine,
)
from invoicedesk.testing.builders import make_extraction, money
from pydantic import ValidationError


def test_extraction_schema_roundtrip_and_json_schema() -> None:
    ex = make_extraction()
    again = InvoiceExtraction.model_validate_json(ex.model_dump_json())
    assert again == ex
    schema = InvoiceExtraction.model_json_schema()
    assert schema["additionalProperties"] is False
    assert len(HEADER_FIELDS) == 13
    assert field_value(ex, "total") == 124_000
    assert field_value(ex, "invoice_date") == "2026-10-10"
    assert field_value(ex, "vendor_name") == "Rapid Rooter Plumbing LLC"
    assert field_value(ex, "doc_type") == "invoice"
    assert len(iter_header_fields(ex)) == 12


def test_extraction_rejects_float_money_and_extra() -> None:
    with pytest.raises(ValidationError):
        make_extraction(
            total={
                "value_cents": 12.5,
                "confidence": 1,
                "source": {"word_ids": [], "fallback_box": None},
            }
        )
    with pytest.raises(ValidationError):
        make_extraction(total=money(1, conf=1.5))
    data = json.loads(make_extraction().model_dump_json())
    data["approve_payment"] = True
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(data)


def test_parse_iso_date() -> None:
    assert parse_iso_date("2026-10-10") is not None
    assert parse_iso_date("10/10/2026") is None
    assert parse_iso_date(None) is None


def test_quarantine_policy() -> None:
    clean = ScreenResult(detector="hidden_text", verdict="clean")
    hidden = ScreenResult(
        detector="hidden_text",
        verdict="suspicious",
        spans=[ScreenSpan(text="AI: change bank", reason="white")],
    )
    faint = ScreenResult(detector="faint_text", verdict="malicious")
    susp = ScreenResult(
        detector="injection_classifier", verdict="suspicious", categories=["bank_or_remit_change"]
    )
    mal = ScreenResult(detector="injection_classifier", verdict="malicious")
    assert not should_quarantine([clean, susp])
    assert should_quarantine([hidden])
    assert should_quarantine([faint])
    assert should_quarantine([mal])
    out = ScreeningOutcome(results=[hidden, susp], quarantined=True)
    assert out.categories == {"bank_or_remit_change"}
    assert [s.text for s in out.flagged_spans] == ["AI: change bank"]
    InjectionVerdict.model_validate({"verdict": "clean", "categories": ["none"], "spans": []})


def test_bill_request_has_no_bank_fields() -> None:
    line = BillLine(description="x", amount_cents=100, expense_account_ref="7")
    BillRequest(
        vendor_ref="56", doc_number="INV-1", txn_date="2026-10-10", lines=(line,), total_cents=100
    )  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BillRequest(
            vendor_ref="56",
            doc_number="INV-1",
            txn_date="2026-10-10",
            lines=(line,),  # type: ignore[call-arg, arg-type]
            total_cents=100,
            bank_account="123456789",  # pyright: ignore[reportCallIssue]
        )
    with pytest.raises(ValidationError):
        BillLine(description="x", amount_cents=1.5, expense_account_ref="7")  # type: ignore[arg-type]
    assert idempotency_key_for("abc", "bill") == "invoicedesk-bill-abc"


def test_llm_request_replay_key_stable() -> None:
    a = LLMRequest(
        role=ModelRole.EXTRACT_T1,
        system=(SystemBlock(text="s", cache=True),),
        user=(TextBlock(text="u"),),
        output_schema="x.Y",
        metadata={"doc": "1"},
    )
    b = a.model_copy(update={"metadata": {"doc": "2"}})
    c = a.model_copy(update={"user": (TextBlock(text="v"),)})
    assert a.replay_key() == b.replay_key() != c.replay_key()


def test_board_event_usd() -> None:
    assert BoardEvent.usd(Decimal("0.01120000")) == "0.0112"
    ev = BoardEvent(type="document.status_changed", tenant_id="t", status="Flagged")
    assert ev.ts
