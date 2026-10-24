"""CLAUDE.md invariant 4: the posting adapter can only create a Bill and attach a file."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import re
from pathlib import Path

import invoicedesk.accounting as accounting
import pytest
from invoicedesk.accounting import QuickBooksAdapter, quickbooks
from invoicedesk.accounting.fake_server import T0, make_bill
from invoicedesk.contracts.accounting import BillLine, BillRequest
from pydantic import ValidationError

FORBIDDEN = re.compile(
    r"payment|vendor|bank|remit|delete|void|update|transport|http|query|request", re.IGNORECASE
)


def public_names(obj: object) -> set[str]:
    return {n for n in dir(obj) if not n.startswith("_")}


def test_public_surface_is_exactly_two_operations(transport) -> None:
    a = QuickBooksAdapter(transport)
    assert public_names(QuickBooksAdapter) == {"system", "create_bill", "attach_file"}
    assert public_names(a) == {"system", "create_bill", "attach_file"}
    for name in public_names(a):
        assert not FORBIDDEN.search(name), name
    # the transport is name-mangled private, not a public attribute
    assert not any(isinstance(getattr(a, n), type(transport)) for n in public_names(a))


def test_method_signatures_are_the_contract() -> None:
    sig = inspect.signature(QuickBooksAdapter.create_bill)
    assert list(sig.parameters) == ["self", "bill", "idempotency_key"]
    assert sig.parameters["idempotency_key"].kind is inspect.Parameter.KEYWORD_ONLY
    sig = inspect.signature(QuickBooksAdapter.attach_file)
    assert list(sig.parameters) == [
        "self",
        "bill_external_id",
        "filename",
        "content_type",
        "data",
        "idempotency_key",
    ]


def test_adapter_module_has_no_payment_vendor_or_delete_endpoints() -> None:
    src = Path(quickbooks.__file__).read_text()
    tree = ast.parse(src)
    docstrings = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    lowered = " ".join(strings).lower()
    for bad in (
        "billpayment",
        "operation",
        "/vendor",
        "vendor/",
        "bankaccount",
        "purchase",
        "journalentry",
        "sparse",
    ):
        assert bad not in lowered, bad
    # the only HTTP paths the adapter names
    paths = set(re.findall(r'request\(\s*"(GET|POST)",\s*"([a-z/]+)"', src))
    assert paths == {("POST", "bill"), ("POST", "upload"), ("GET", "query")}
    # and it never imports the demo-only deleter
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert not any("sandbox_admin" in m for m in imported), imported


def test_sandbox_admin_is_not_exported() -> None:
    assert "QboSandboxAdmin" not in accounting.__all__
    assert not hasattr(accounting, "QboSandboxAdmin")


def test_http_calls_during_a_full_post(fake_qbo, transport) -> None:
    """Create + attach + replay + duplicate lookup: every call hits an allowed endpoint."""
    server, _ = fake_qbo
    server.duplicate_docnumber_check = True
    a = QuickBooksAdapter(transport, clock=lambda: T0)
    bill = a.create_bill(make_bill(), idempotency_key="k-bill")
    a.create_bill(make_bill(), idempotency_key="k-bill")
    server.cache.clear()
    QuickBooksAdapter(transport, clock=lambda: T0 + dt.timedelta(hours=1)).create_bill(
        make_bill(), idempotency_key="k-bill"
    )
    a.attach_file(
        bill.external_id,
        filename="i.pdf",
        content_type="application/pdf",
        data=b"%PDF",
        idempotency_key="k-att",
    )
    allowed = {("POST", "bill"), ("POST", "upload"), ("GET", "query")}
    seen = set()
    for r in [*server.requests, *server.lookups]:
        path = r.url.path.rsplit("/", 1)[-1]
        seen.add((r.method, path))
        assert r.url.host == "sandbox-quickbooks.api.intuit.com"
        assert "operation" not in r.url.params
        if r.method == "POST":
            assert r.url.params.get("requestid"), "every write carries a requestid"
        if path == "query":
            assert re.fullmatch(
                r"select [A-Za-z, ]+ from Bill where DocNumber = '.*'", r.url.params["query"]
            )
    assert seen <= allowed
    assert seen == allowed


@pytest.mark.parametrize(
    "extra",
    [
        {"bank_account": "000123456789"},
        {"remit_to": "PO Box 1"},
        {"routing_number": "021000021"},
        {"payment": {"amount_cents": 1}},
        {"vendor_update": {"BankAccount": "x"}},
        {"BillPayment": True},
    ],
)
def test_bill_request_rejects_extra_fields(extra: dict) -> None:
    base = make_bill().model_dump()
    with pytest.raises(ValidationError):
        BillRequest(**{**base, **extra})


def test_bill_line_rejects_extra_fields_and_floats() -> None:
    with pytest.raises(ValidationError):
        BillLine(description="x", amount_cents=100, expense_account_ref="7", bank="1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BillLine(description="x", amount_cents=1.5, expense_account_ref="7")  # type: ignore[arg-type]


def test_bill_request_has_no_bank_fields() -> None:
    names = set(BillRequest.model_fields) | set(BillLine.model_fields)
    assert not {n for n in names if re.search(r"bank|remit|routing|account_number|iban", n)}
