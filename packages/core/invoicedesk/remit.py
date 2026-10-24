"""Remit-to / bank-detail fingerprints.

The vendor master never stores raw bank numbers: it stores ``remit_to_hash`` (a salted SHA-256
of the normalized remit-to details) and ``bank_last4``. Seed data, rules (R6) and the review UI
all use :func:`remit_fingerprint` so that they agree on normalization.
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["extract_account_digits", "normalize_remit", "remit_fingerprint"]

_DEFAULT_SALT = "invoicedesk-remit-v1"
_DIGITS = re.compile(r"\d")
_ROUTING_HINT = re.compile(r"(routing|aba|rtn)\D{0,12}(\d[\d -]{7,12}\d)", re.IGNORECASE)
_ACCOUNT_HINT = re.compile(r"(acct|account|a/c)\D{0,12}(\d[\d -]{3,20}\d)", re.IGNORECASE)


def extract_account_digits(text: str) -> tuple[str | None, str | None]:
    """Best-effort ``(routing_digits, account_digits)`` from free text such as
    ``"Routing 021000021 Acct 000123456789"``. Either may be ``None``."""
    r = _ROUTING_HINT.search(text)
    a = _ACCOUNT_HINT.search(text)
    routing = "".join(_DIGITS.findall(r.group(2))) if r else None
    account = "".join(_DIGITS.findall(a.group(2))) if a else None
    if routing is None and account is None:
        runs = ["".join(_DIGITS.findall(x)) for x in re.findall(r"\d[\d -]{5,}\d", text)]
        runs = [x for x in runs if len(x) >= 6]
        if len(runs) >= 2:
            routing, account = runs[0], runs[1]
        elif runs:
            account = runs[0]
    return routing, account


def normalize_remit(text: str) -> str:
    """Canonical form: ``"<routing>|<account>"`` digits only when found, else lowercase
    alphanumerics. Payee names and punctuation do not change the fingerprint."""
    routing, account = extract_account_digits(text)
    if routing or account:
        return f"{routing or ''}|{account or ''}"
    return re.sub(r"[^a-z0-9]", "", text.lower())


def remit_fingerprint(text: str | None, *, salt: str = _DEFAULT_SALT) -> str | None:
    """Salted SHA-256 hex of :func:`normalize_remit` (``None`` for empty input)."""
    if text is None or not text.strip():
        return None
    norm = normalize_remit(text)
    if not norm:
        return None
    return hashlib.sha256(f"{salt}:{norm}".encode()).hexdigest()
