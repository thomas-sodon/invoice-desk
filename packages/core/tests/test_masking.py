from invoicedesk.masking import last4, mask_number, mask_payload, mask_text


def test_mask_number() -> None:
    assert mask_number("021000021") == "•••••0021"
    assert mask_number("12-34 5678") == "••••5678"
    assert mask_number("123") == "•••"
    assert mask_number(None) is None
    assert mask_number(123456789) == "•••••6789"
    assert last4("ACCT 0001-2345") == "2345"


def test_mask_text_keeps_dates_money_invoice_numbers() -> None:
    s = "Pay to routing 021000021 acct 000123456789 on 2026-10-10 total $1,240.00 INV-2231"
    out = mask_text(s)
    assert out is not None
    assert "021000021" not in out
    assert "000123456789" not in out
    assert "•••••0021" in out
    assert "••••••••6789" in out
    assert "2026-10-10" in out
    assert "$1,240.00" in out
    assert "INV-2231" in out
    assert mask_text(None) is None


def test_mask_payload_nested() -> None:
    p = {
        "bank_account": "000123456789",
        "routing": 21000021,
        "remit_to_bank_details": "First Test Bank acct 9876543210",
        "note": "call 5551234567 now",
        "items": [{"account_number": "ABC12345"}],
        "count": 3,
        "flag": True,
        "account_short": "12",
    }
    out = mask_payload(p)
    assert out["bank_account"] == "••••••••6789"
    assert out["routing"] == "••••0021"
    assert "9876543210" not in out["remit_to_bank_details"]
    assert out["note"] == "call ••••••4567 now"
    assert out["items"][0]["account_number"] == "••••2345"
    assert out["count"] == 3
    assert out["flag"] is True
    assert out["account_short"] == "12"
    assert mask_payload(b"acct 1234567890") == "acct ••••••7890"


def test_amounts_are_not_masked_but_accounts_still_are() -> None:
    assert mask_text("cost 0.00012345 USD") == "cost 0.00012345 USD"
    assert mask_text("rate 3,1234567") == "rate 3,1234567"
    assert mask_text("Acct.000123456789") == "Acct.••••••••6789"
    assert mask_text("Routing 021000021. Account 000123456789.") == (
        "Routing •••••0021. Account ••••••••6789."
    )
    out = mask_payload(
        {"total_cents": "124000", "cost_usd": "0.00012345", "note": "pay 000123456789"}
    )
    assert out == {"total_cents": "124000", "cost_usd": "0.00012345", "note": "pay ••••••••6789"}
    # a bank key is masked whatever its name ends with, and a non-amount under an amount key is
    # still scrubbed
    assert mask_payload({"bank_account_usd": "000123456789"}) == {
        "bank_account_usd": "••••••••6789"
    }
    assert mask_payload({"total_cents": "acct 000123456789"}) == {
        "total_cents": "acct ••••••••6789"
    }
