from invoicedesk.remit import extract_account_digits, normalize_remit, remit_fingerprint


def test_extract_digits() -> None:
    assert extract_account_digits("Routing: 021000021, Account # 0001234567") == (
        "021000021",
        "0001234567",
    )
    assert extract_account_digits("021000021 / 0001234567") == ("021000021", "0001234567")
    assert extract_account_digits("acct 1234567") == (None, "1234567")
    assert extract_account_digits("send to 9988776655") == (None, "9988776655")
    assert extract_account_digits("no numbers here") == (None, None)


def test_fingerprint_ignores_formatting() -> None:
    a = remit_fingerprint("Routing 021000021 Acct 000-123-4567")
    b = remit_fingerprint("ABA: 021000021  account no. 0001234567  (Rapid Rooter)")
    c = remit_fingerprint("Routing 021000021 Acct 0001234568")
    assert a == b
    assert a != c
    assert remit_fingerprint(None) is None
    assert remit_fingerprint("   ") is None
    assert remit_fingerprint("---") is None
    assert normalize_remit("Pay by Check") == "paybycheck"
    assert remit_fingerprint("x", salt="s1") != remit_fingerprint("x", salt="s2")
