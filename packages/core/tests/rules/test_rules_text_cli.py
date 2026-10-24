from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from invoicedesk.contracts.tenant_data import TenantData
from invoicedesk.rules import cli
from invoicedesk.rules._common import vendor_display_name
from invoicedesk.rules._text import (
    basic_norm,
    invoice_number_key,
    is_mixed_script,
    skeleton,
    work_order_key,
)
from invoicedesk.rules.variance import variance_headline, variance_sentence
from invoicedesk.testing.builders import make_extraction, money

Universe = Callable[..., TenantData]
REPO = Path(__file__).resolve().parents[4]


def test_basic_norm() -> None:
    assert basic_norm("Rapid Rooter Plumbing, L.L.C.") == "rapid rooter plumbing l l c"
    assert basic_norm("Rapid Rooter Plumbing LLC") == "rapid rooter plumbing"
    assert basic_norm("Smith & Sons Co.") == "smith and sons"
    assert basic_norm("LLC") == ""


def test_skeleton_folds_confusables() -> None:
    assert skeleton("Rаpid Rооter") == skeleton("Rapid Rooter")  # Cyrillic а, о
    assert skeleton("Rapid R00ter") == skeleton("Rapid Rooter")
    assert skeleton("Modern Plumbing") == skeleton("Modem Plumbing")  # rn -> m
    assert skeleton("Café") == skeleton("Cafe")
    assert skeleton("Wave") == skeleton("VVave")


def test_mixed_script() -> None:
    assert is_mixed_script("Rаpid")
    assert is_mixed_script("Αcme Pipes")  # Greek capital alpha
    assert not is_mixed_script("Rapid 123")
    assert not is_mixed_script("Rapid 株式会社")  # other scripts are not homoglyph mixes
    assert not is_mixed_script("Зебра")  # all Cyrillic is not mixed


def test_invoice_and_wo_keys() -> None:
    assert invoice_number_key("INV-002231") == invoice_number_key("#2231") == "2231"
    assert invoice_number_key("Invoice No. 2231") == "2231"
    assert invoice_number_key(None) is None and invoice_number_key("INV") is None
    assert work_order_key("Work Order #01187") == work_order_key("wo-1187") == "1187"
    assert work_order_key(None) is None and work_order_key("WO") is None


def test_variance_templates() -> None:
    kw = {
        "billed_cents": 124_000,
        "estimate_cents": 90_000,
        "unit_label": "12B",
        "wo_number": "WO-1187",
    }
    assert variance_sentence(vendor="Rapid Rooter", **kw) == (
        "Rapid Rooter invoice is $340.00 over the approved estimate ($900.00) for Unit 12B, WO-1187."
    )
    assert variance_headline(**kw) == "$340.00 over approved estimate for Unit 12B (WO-1187)"
    with pytest.raises(TypeError):
        variance_headline(billed_cents=1.5, estimate_cents=1, unit_label="x", wo_number="y")  # type: ignore[arg-type]


def test_vendor_display_name() -> None:
    assert vendor_display_name("Rapid Rooter Plumbing LLC") == "Rapid Rooter Plumbing"
    assert vendor_display_name("Northgate Builders, Inc.") == "Northgate Builders"
    assert vendor_display_name("LLC") == "LLC"


# ---------------------------------------------------------------- CLI


def _write(tmp: Path, name: str, data: object) -> str:
    p = tmp / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_cli_table_and_json(
    tmp_path: Path, universe: Universe, capsys: pytest.CaptureFixture[str]
) -> None:
    ex = _write(tmp_path, "ex.json", make_extraction().model_dump(mode="json"))
    td = _write(tmp_path, "td.json", universe().model_dump(mode="json"))
    args = [ex, td, "--tenants-dir", str(REPO / "tenants"), "--today", "2026-10-24", "--raw-conf"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "R1   vendor_known       PASS" in out and "Route: ReadyToPay" in out

    assert cli.main([*args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "ReadyToPay" and len(payload["results"]) == 10


def test_cli_optional_inputs(
    tmp_path: Path, universe: Universe, capsys: pytest.CaptureFixture[str]
) -> None:
    ex = _write(tmp_path, "ex.json", make_extraction(total=money(900_000)).model_dump(mode="json"))
    td = _write(tmp_path, "td.json", universe().model_dump(mode="json"))
    conf = _write(tmp_path, "conf.json", {"total": 0.99})
    scr = _write(tmp_path, "scr.json", {"results": [], "quarantined": True})
    doc = _write(tmp_path, "doc.json", {"document_id": "d1", "sha256": "x", "uploaded_by": "u-up"})
    appr = _write(
        tmp_path,
        "appr.json",
        [{"approver_id": "u-boss", "kind": "over_cap", "decision": "approve"}],
    )
    args = [
        ex,
        td,
        "--config",
        str(REPO / "tenants" / "maple_row" / "rules.yaml"),
        "--conf",
        conf,
        "--screening",
        scr,
        "--document",
        doc,
        "--approvals",
        appr,
        "--json",
    ]
    assert cli.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {r["rule_id"]: r for r in payload["results"]}
    assert payload["route"] == "Flagged"
    assert by_id["R7"]["passed"] is True  # over cap but approved by a non-uploader
    assert by_id["R9"]["passed"] is False  # only total had a calibrated confidence
    assert by_id["R10"]["severity"] == "flag"


def test_cli_without_conf_holds_r9(
    tmp_path: Path,
    universe: Universe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = _write(tmp_path, "ex.json", make_extraction().model_dump(mode="json"))
    td = _write(tmp_path, "td.json", universe().model_dump(mode="json"))
    monkeypatch.setattr(
        "sys.argv", ["invoicedesk validate", ex, td, "--tenants-dir", str(REPO / "tenants")]
    )
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "R9   confidence         REVIEW" in out
