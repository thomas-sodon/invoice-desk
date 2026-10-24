"""QuickBooksAdapter against the respx-mounted fake QBO sandbox."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal

import httpx
import pytest
from invoicedesk.accounting import QuickBooksAdapter
from invoicedesk.accounting.fake_server import (
    BASE,
    REALM,
    T0,
    StaticTokens,
    fault,
    make_bill,
    parse_multipart,
)
from invoicedesk.accounting.quickbooks import QboTotalMismatch, marker_for, request_id_for
from invoicedesk.accounting.transport import (
    MINOR_VERSION,
    QboApiError,
    QboTransport,
    cents_to_decimal,
    decimal_to_cents,
    encode_json,
)
from invoicedesk.contracts.accounting import (
    AccountingAdapter,
    AccountingError,
    BillLine,
    idempotency_key_for,
)

KEY = "invoicedesk-bill-0001"


def adapter(transport: QboTransport, now: dt.datetime = T0) -> QuickBooksAdapter:
    return QuickBooksAdapter(transport, clock=lambda: now)


def json_body(req: httpx.Request) -> dict:
    return json.loads(req.content)


# ---------------------------------------------------------------- money encoding
@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "0.00"),
        (5, "0.05"),
        (10, "0.10"),
        (124000, "1240.00"),
        (-1999, "-19.99"),
        (123456789012345, "1234567890123.45"),
    ],
)
def test_cents_to_decimal_exact(cents: int, text: str) -> None:
    d = cents_to_decimal(cents)
    assert format(d, "f") == text
    assert decimal_to_cents(d) == cents
    assert encode_json({"Amount": d}) == f'{{"Amount":{text}}}'


def test_money_encoding_refuses_floats_and_subcents() -> None:
    with pytest.raises(TypeError):
        cents_to_decimal(12.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cents_to_decimal(True)
    with pytest.raises(TypeError):
        encode_json({"Amount": 1.1})
    with pytest.raises(ValueError):
        decimal_to_cents(Decimal("1.005"))
    with pytest.raises(ValueError):
        encode_json({"Amount": Decimal("NaN")})


def test_encode_json_marker_collision_is_impossible() -> None:
    text = encode_json({"a": "__dec_x_0__", "b": Decimal("1.50")})
    assert json.loads(text) == {"a": "__dec_x_0__", "b": 1.5}


# ---------------------------------------------------------------- create_bill
def test_create_bill_request_shape(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    res = adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert res.system == "qbo"
    assert res.status == "created"
    assert res.external_id == "145"
    assert res.raw_sync_token == "0"

    (req,) = server.requests
    assert req.method == "POST"
    assert req.url.path == f"/v3/company/{REALM}/bill"
    assert req.url.params["requestid"] == KEY
    assert req.url.params["minorversion"] == str(MINOR_VERSION)
    assert req.headers["authorization"] == "Bearer access-1"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["accept"] == "application/json"
    raw = req.content.decode()
    # exact decimal literals, never float renderings like 980.0
    assert '"Amount":980.00' in raw and '"Amount":260.00' in raw
    body = json_body(req)
    assert body["VendorRef"] == {"value": "56"}
    assert body["TxnDate"] == "2026-10-02"
    assert body["DueDate"] == "2026-11-01"
    assert body["DocNumber"] == "INV-2231"
    assert body["PrivateNote"] == f"WO-1187 Unit 12B | {marker_for(KEY)}"
    assert body["Line"][0] == {
        "DetailType": "AccountBasedExpenseLineDetail",
        "Amount": 980.0,
        "Description": "Replace kitchen faucet, Unit 12B",
        "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}},
    }
    assert set(body) == {"VendorRef", "TxnDate", "DueDate", "DocNumber", "PrivateNote", "Line"}
    assert server.bills["145"]["TotalAmt"] == Decimal("1240.00")


def test_due_date_optional_and_no_memo(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    adapter(transport).create_bill(make_bill(due_date=None, memo=None), idempotency_key=KEY)
    body = json_body(server.requests[0])
    assert "DueDate" not in body
    assert body["PrivateNote"] == marker_for(KEY)


def test_odd_cents_round_trip_exactly(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    lines = (
        BillLine(description="a", amount_cents=1, expense_account_ref="7"),
        BillLine(description="b", amount_cents=33333, expense_account_ref="7"),
        BillLine(description="c", amount_cents=10, expense_account_ref="7"),
    )
    res = adapter(transport).create_bill(
        make_bill(lines=lines, total_cents=33344), idempotency_key=KEY
    )
    assert res.status == "created"
    raw = server.requests[0].content.decode()
    assert '"Amount":0.01' in raw and '"Amount":333.33' in raw and '"Amount":0.10' in raw


def test_long_idempotency_key_maps_to_deterministic_requestid(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    key = idempotency_key_for("3f2b8c1e-0d7a-4c55-9a0e-6f1d2b3c4d5e", "bill")
    assert len(key) > 50
    rid = request_id_for(key)
    assert rid == request_id_for(key) and len(rid) <= 50
    adapter(transport).create_bill(make_bill(), idempotency_key=key)
    assert server.requests[0].url.params["requestid"] == rid
    assert marker_for(key) in json_body(server.requests[0])["PrivateNote"]
    with pytest.raises(ValueError):
        request_id_for("")


def test_lines_must_sum_to_total_no_http(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(total_cents=124001), idempotency_key=KEY)
    assert not ei.value.retryable
    with pytest.raises(AccountingError):
        adapter(transport).create_bill(make_bill(lines=(), total_cents=0), idempotency_key=KEY)
    assert server.requests == []


def test_account_numbers_masked_in_payload(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    bill = make_bill(
        memo="remit to acct 000123456789",
        lines=(
            BillLine(
                description="ACH 021000021 / 9876543210",
                amount_cents=124000,
                expense_account_ref="7",
            ),
        ),
    )
    adapter(transport).create_bill(bill, idempotency_key=KEY)
    raw = server.requests[0].content.decode()
    assert "000123456789" not in raw and "021000021" not in raw and "9876543210" not in raw
    assert "6789" in raw and "3210" in raw


# ---------------------------------------------------------------- idempotency
def test_retry_same_key_returns_same_bill_never_second_effect(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    a = adapter(transport)
    first = a.create_bill(make_bill(), idempotency_key=KEY)
    second = a.create_bill(make_bill(), idempotency_key=KEY)
    assert first.status == "created"
    assert second.status == "already_exists"
    assert second.external_id == first.external_id
    assert server.effects == 1 and len(server.bills) == 1
    assert [r.url.params["requestid"] for r in server.requests] == [KEY, KEY]


def test_replay_after_crash_new_adapter_instance(fake_qbo, transport) -> None:
    """A worker crashed after QBO created the bill; the job is retried ten minutes later."""
    server, _ = fake_qbo
    first = adapter(transport, now=T0).create_bill(make_bill(), idempotency_key=KEY)
    later = adapter(transport, now=T0 + dt.timedelta(minutes=10))
    again = later.create_bill(make_bill(), idempotency_key=KEY)
    assert again.status == "already_exists" and again.external_id == first.external_id
    assert server.effects == 1


def test_timeout_after_server_side_create_retries_with_same_requestid(
    fake_qbo, transport, sleeps
) -> None:
    server, _ = fake_qbo

    def boom(request: httpx.Request) -> None:
        raise httpx.ReadTimeout("read timed out", request=request)

    server.after.append(boom)  # the bill IS created, but the response is lost
    res = adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert res.external_id == "145"
    assert server.effects == 1 and len(server.bills) == 1
    assert [r.url.params["requestid"] for r in server.requests] == [KEY, KEY]
    assert sleeps == [0.5]


def test_different_keys_create_different_bills(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    a = adapter(transport)
    r1 = a.create_bill(make_bill(), idempotency_key="k-1")
    r2 = a.create_bill(make_bill(doc_number="INV-2232"), idempotency_key="k-2")
    assert r1.external_id != r2.external_id and server.effects == 2


def test_duplicate_docnumber_ours_resolves_to_already_exists(fake_qbo, transport) -> None:
    """QBO's requestid cache expired; DocNumber check fires; the marker proves it's ours."""
    server, _ = fake_qbo
    server.duplicate_docnumber_check = True
    first = adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    server.cache.clear()
    again = adapter(transport, now=T0).create_bill(make_bill(), idempotency_key=KEY)
    assert again.status == "already_exists" and again.external_id == first.external_id
    assert server.effects == 1
    q = server.requests[-1]
    assert q.method == "GET" and q.url.path.endswith("/query")
    assert "from Bill where DocNumber = 'INV-2231'" in q.url.params["query"]


def test_duplicate_docnumber_not_ours_is_not_retryable(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    server.duplicate_docnumber_check = True
    adapter(transport).create_bill(make_bill(), idempotency_key="someone-else")
    with pytest.raises(QboApiError) as ei:
        adapter(transport).create_bill(make_bill(doc_number="INV-2231"), idempotency_key=KEY)
    assert not ei.value.retryable
    assert "6140" in ei.value.codes
    assert server.effects == 1


def test_docnumber_quotes_are_escaped_in_query(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    server.duplicate_docnumber_check = True
    a = adapter(transport)
    a.create_bill(make_bill(doc_number="O'BRIEN-1"), idempotency_key=KEY)
    server.cache.clear()
    res = adapter(transport).create_bill(make_bill(doc_number="O'BRIEN-1"), idempotency_key=KEY)
    assert res.status == "already_exists"
    assert "DocNumber = 'O\\'BRIEN-1'" in server.requests[-1].url.params["query"]


def test_total_mismatch_raises_with_external_id(fake_qbo, transport) -> None:
    server, _ = fake_qbo

    def tax(request: httpx.Request) -> httpx.Response | None:
        server.before.clear()
        resp = server.handle(request)
        body = json.loads(resp.content)
        body["Bill"]["TotalAmt"] = 1339.20  # vendor default tax applied by QBO
        return httpx.Response(200, json=body)

    server.before.append(tax)
    with pytest.raises(QboTotalMismatch) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert ei.value.external_id == "145" and not ei.value.retryable


# ---------------------------------------------------------------- error mapping
def test_503_then_success_is_retried(fake_qbo, transport, sleeps) -> None:
    server, _ = fake_qbo
    server.before.append(
        lambda r: httpx.Response(503, json=fault("0", "Service Unavailable", ftype="SystemFault"))
    )
    res = adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert res.status == "created" and len(server.requests) == 2 and sleeps == [0.5]


def test_429_exhausted_is_retryable(fake_qbo, transport, sleeps) -> None:
    server, _ = fake_qbo
    for _ in range(3):
        server.before.append(
            lambda r: httpx.Response(
                429, json=fault("003001", "ThrottleExceeded", ftype="ThrottleExceeded")
            )
        )
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert ei.value.retryable and ei.value.status_code == 429
    assert len(server.requests) == 3 and sleeps == [0.5, 1.0]
    assert server.effects == 0


def test_500_exhausted_is_retryable(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    for _ in range(3):
        server.before.append(lambda r: httpx.Response(500, text="<html>oops</html>"))
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert ei.value.retryable and ei.value.status_code == 500


def test_connection_error_is_retryable(fake_qbo, transport) -> None:
    server, _ = fake_qbo

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    server.before.extend([down, down, down])
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert ei.value.retryable and ei.value.status_code is None


def test_400_validation_is_not_retryable_and_not_retried(fake_qbo, transport, sleeps) -> None:
    server, _ = fake_qbo
    server.before.append(
        lambda r: httpx.Response(
            400,
            json=fault(
                "2500",
                "Invalid Reference Id",
                "Invalid Reference Id : Vendor assigned to this transaction has been deleted.",
            ),
        )
    )
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert not ei.value.retryable and ei.value.status_code == 400
    assert "[2500] Invalid Reference Id" in str(ei.value)
    assert len(server.requests) == 1 and sleeps == []


def test_fault_in_200_body_is_an_error(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    server.before.append(
        lambda r: httpx.Response(
            200, json=fault("6000", "A business validation error has occurred")
        )
    )
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert not ei.value.retryable


def test_401_refreshes_once_then_succeeds(fake_qbo, tokens: StaticTokens, sleeps) -> None:
    server, _ = fake_qbo
    server.valid_tokens = {"access-2"}
    t = QboTransport(realm_id=REALM, tokens=tokens, http=httpx.Client(), sleep=sleeps.append)
    res = adapter(t).create_bill(make_bill(), idempotency_key=KEY)
    assert res.status == "created" and tokens.refreshes == 1
    assert [r.headers["authorization"] for r in server.requests] == [
        "Bearer access-1",
        "Bearer access-2",
    ]
    assert sleeps == []


def test_401_after_refresh_is_not_retryable(fake_qbo, transport, tokens) -> None:
    server, _ = fake_qbo
    server.valid_tokens = set()
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert not ei.value.retryable and ei.value.status_code == 401
    assert "after a token refresh" in str(ei.value)
    assert tokens.refreshes == 1 and len(server.requests) == 2


def test_logs_are_masked_and_tokenless(fake_qbo, transport, caplog) -> None:
    server, _ = fake_qbo
    caplog.set_level(logging.DEBUG)
    server.before.append(
        lambda r: httpx.Response(
            400, json=fault("2020", "Required param missing", "BankAccount 000123456789 is invalid")
        )
    )
    with pytest.raises(AccountingError) as ei:
        adapter(transport).create_bill(make_bill(), idempotency_key=KEY)
    assert "000123456789" not in str(ei.value) and "6789" in str(ei.value)
    adapter(transport).create_bill(make_bill(), idempotency_key="k-2")
    text = caplog.text + str(ei.value)
    assert "access-1" not in text
    assert not re.search(r"\d{6,}", caplog.text.replace(REALM, ""))


# ---------------------------------------------------------------- attach_file
def _posted(transport: QboTransport) -> tuple[QuickBooksAdapter, str]:
    a = adapter(transport)
    return a, a.create_bill(make_bill(), idempotency_key=KEY).external_id


def test_attach_file_multipart_format(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    a, bill_id = _posted(transport)
    pdf = b"%PDF-1.7\n...binary\x00\xff..."
    res = a.attach_file(
        bill_id,
        filename="scans/INV 2231 (photo).pdf",
        content_type="application/pdf",
        data=pdf,
        idempotency_key="invoicedesk-attach-0001",
    )
    assert res.status == "created" and res.external_id.isdigit()
    req = server.requests[-1]
    assert req.method == "POST"
    assert req.url.path == f"/v3/company/{REALM}/upload"
    assert req.url.params["requestid"] == "invoicedesk-attach-0001"
    assert req.url.params["minorversion"] == str(MINOR_VERSION)
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = parse_multipart(req)
    assert [p["name"] for p in parts] == ["file_metadata_01", "file_content_01"]
    meta_part, file_part = parts
    assert meta_part["content_type"] == "application/json"
    assert meta_part["filename"] == "attachment.json"
    meta = json.loads(meta_part["data"])
    assert meta["AttachableRef"] == [
        {"EntityRef": {"type": "Bill", "value": bill_id}, "IncludeOnSend": False}
    ]
    assert meta["FileName"] == "INV 2231 _photo_.pdf"
    assert meta["ContentType"] == "application/pdf"
    assert file_part["filename"] == "INV 2231 _photo_.pdf"
    assert file_part["content_type"] == "application/pdf"
    assert file_part["data"] == pdf
    assert server.attachables[res.external_id]["Size"] == len(pdf)


def test_attach_retry_is_idempotent(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    a, bill_id = _posted(transport)
    kw = {
        "filename": "x.png",
        "content_type": "image/png",
        "data": b"\x89PNG..",
        "idempotency_key": "att-1",
    }
    r1 = a.attach_file(bill_id, **kw)  # type: ignore[arg-type]
    r2 = a.attach_file(bill_id, **kw)  # type: ignore[arg-type]
    r3 = adapter(transport, now=T0 + dt.timedelta(hours=1)).attach_file(bill_id, **kw)  # type: ignore[arg-type]
    assert r1.status == "created"
    assert r2.status == r3.status == "already_exists"
    assert r1.external_id == r2.external_id == r3.external_id
    assert len(server.attachables) == 1


def test_attach_fault_in_attachable_response(fake_qbo, transport) -> None:
    a = adapter(transport)
    with pytest.raises(AccountingError) as ei:
        a.attach_file(
            "999",
            filename="x.pdf",
            content_type="application/pdf",
            data=b"%PDF",
            idempotency_key="att-1",
        )
    assert not ei.value.retryable and "Object Not Found" in str(ei.value)


@pytest.mark.parametrize(
    ("bill_id", "ctype", "data"),
    [
        ("12/../vendor", "application/pdf", b"x"),
        ("145", "application/x-msdownload", b"MZ"),
        ("145", "text/html", b"<script>"),
        ("145", "application/pdf", b""),
    ],
)
def test_attach_input_validation_no_http(fake_qbo, transport, bill_id, ctype, data) -> None:
    server, _ = fake_qbo
    with pytest.raises(AccountingError) as ei:
        adapter(transport).attach_file(
            bill_id, filename="x", content_type=ctype, data=data, idempotency_key="a"
        )
    assert not ei.value.retryable
    assert server.requests == []


def test_content_type_parameters_are_normalised(fake_qbo, transport) -> None:
    server, _ = fake_qbo
    a, bill_id = _posted(transport)
    a.attach_file(
        bill_id,
        filename="n.txt",
        content_type="Text/Plain; charset=utf-8",
        data=b"hello",
        idempotency_key="a",
    )
    assert parse_multipart(server.requests[-1])[1]["content_type"] == "text/plain"


def test_adapter_satisfies_protocol(transport) -> None:
    a = adapter(transport)
    assert isinstance(a, AccountingAdapter)
    assert a.system == "qbo"


def test_base_url_constant() -> None:
    assert BASE.startswith("https://sandbox-quickbooks.api.intuit.com/v3/company/")
