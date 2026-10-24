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


def test_glued_account_numbers_and_ibans_are_masked() -> None:
    # OCR of hidden or faint text drops spaces; the number is still an account number.
    cases = {
        "newaccoun000555067890": "newaccoun••••••••7890",
        "sendto:acct000555067890": "sendto:acct••••••••7890",
        "account-000555067890": "account-••••••••7890",
        "ACCT123456789": "ACCT•••••6789",
        "paymentto.newaccoun 000555067890": "paymentto.newaccoun ••••••••7890",
        "IBAN GB82WEST12345698765432": "IBAN ••••••••••••••••••5432",
        "IBAN GB82 WEST 1234 5698 7654 32 today": "IBAN ••••••••••••••••••5432 today",
        "deadbeef000555067890": "deadbeef••••••••7890",
        "on 2026-10-24 555067890": "on 2026-10-24 •••••7890",
        "acct0005-5506-7890": "acct••••••••7890",
        # a short grouped code glued to a word is a file or invoice reference, kept
        "08-handy-heron-26-7200-handwritten.jpg": "08-handy-heron-26-7200-handwritten.jpg",
    }
    for raw, want in cases.items():
        assert mask_text(raw) == want, raw
    for raw in ("000555067890", "123456789", "GB82WEST12345698765432"):
        for text in (f"x{raw}", f"{raw}x", f"-{raw}-", f"acct:{raw}."):
            out = mask_text(text) or ""
            assert raw not in out and raw[-4:] in out, text


def test_ids_hashes_dates_and_amounts_survive() -> None:
    keep = [
        "sha 3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
        "doc 123e4567-e89b-12d3-a456-426614174000",
        "trace 4bf92f3577b34da6a3ce929d0e0e4736",
        "at 2026-10-24T12:30:00.123456Z and 2026-10-24 12:30",
        "cost 0.00012345 USD, rate 3,1234567, total $1,240.00",
        "INV-2231 WO-01187 PO 12345",
    ]
    for s in keep:
        assert mask_text(s) == s, s
    out = mask_payload(
        {
            "phash": "f0e1d2c3b4a59687",
            "word_ids": ["p0_w123456"],
            "requestid": "doc-123456789",
            "document_id": "123456789",
            "note": "ref 123456789",
            # a sensitive key wins over the id exemption
            "bank_account_id": "000123456789",
            # an id key holding free text (whitespace) is scrubbed like any text
            "external_id": "acct 000123456789",
        }
    )
    assert out == {
        "phash": "f0e1d2c3b4a59687",
        "word_ids": ["p0_w123456"],
        "requestid": "doc-123456789",
        "document_id": "123456789",
        "note": "ref •••••6789",
        "bank_account_id": "••••••••6789",
        "external_id": "acct ••••••••6789",
    }


def test_mask_bank_text_masks_five_digit_tokens() -> None:
    from invoicedesk.masking import mask_bank_text

    assert mask_bank_text(None) is None
    assert mask_bank_text("First Test Bank acct 98765 IBAN GB82WEST12345698765432") == (
        "First Test Bank acct •8765 IBAN ••••••••••••••••••5432"
    )
