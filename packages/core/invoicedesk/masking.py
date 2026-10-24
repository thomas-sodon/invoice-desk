"""Masking helpers: bank and account numbers are shown as last-4 only (CLAUDE.md invariant 9).

Use :func:`mask_number` for a single value, :func:`mask_text` to scrub free text (logs, trace
attributes, LLM-quoted spans shown in the UI) and :func:`mask_payload` for JSON-like
structures before they reach the audit log, logs, traces or anonymous UI responses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["SENSITIVE_KEY_RE", "last4", "mask_number", "mask_payload", "mask_text"]

MASK_CHAR = "•"

#: Keys whose values are always masked inside payloads.
SENSITIVE_KEY_RE = re.compile(
    r"(bank|account|acct|routing|aba|iban|swift|bic|remit_to_bank|sort_code|card)", re.IGNORECASE
)

# A run of 6+ digits, optionally separated by spaces or dashes (account/routing numbers, IBAN
# bodies). Dates (YYYY-MM-DD), money (1,240.00), decimal fractions (a cost of 0.00012345) and
# short invoice numbers stay untouched. Runs glued to letters ("ACCT123456789") are not
# matched here because hashes and ids pass through this function; bank fields are masked by
# key in :func:`mask_payload` whatever their shape.
_DIGIT_RUN_RE = re.compile(r"(?<![\w-])(?<!\d[.,])(?:[A-Z]{2}\d{2}[ ]?)?\d(?:[ -]?\d){5,}(?![\w-])")
#: Keys whose string values are amounts, not account numbers (integer cents, USD cost).
_AMOUNT_KEY_RE = re.compile(r"(_cents|_usd)$", re.IGNORECASE)
_AMOUNT_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def last4(value: str) -> str:
    """Return the last four alphanumerics of ``value`` ("" if none)."""
    chars = [c for c in value if c.isalnum()]
    return "".join(chars[-4:])


def mask_number(value: str | int | None) -> str | None:
    """``"021000021"`` -> ``"•••••0021"``. ``None`` stays ``None``. Short values are fully masked
    except the last four characters (so a value of 4 or fewer chars is masked entirely)."""
    if value is None:
        return None
    s = str(value)
    chars = [c for c in s if c.isalnum()]
    if len(chars) <= 4:
        return MASK_CHAR * len(chars)
    return MASK_CHAR * (len(chars) - 4) + "".join(chars[-4:])


def mask_text(text: str | None) -> str | None:
    """Mask every long digit run (6+ digits) in free text to its last 4."""
    if text is None:
        return None
    return _DIGIT_RUN_RE.sub(_mask_match, text)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mask_match(m: re.Match[str]) -> str:
    s = m.group(0)
    if _ISO_DATE_RE.match(s):
        return s  # ISO dates are not account numbers
    return mask_number(s) or ""


def mask_payload(obj: Any, *, _key: str | None = None) -> Any:
    """Recursively mask a JSON-like structure.

    Values under sensitive keys (bank/account/routing/...) are masked to last 4 whatever their
    shape; every other string is passed through :func:`mask_text`.
    """
    sensitive = _key is not None and bool(SENSITIVE_KEY_RE.search(_key))
    if (
        not sensitive
        and _key is not None
        and isinstance(obj, str)
        and _AMOUNT_KEY_RE.search(_key)
        and _AMOUNT_VALUE_RE.match(obj)
    ):
        return obj  # "124000" cents / "0.00012345" USD: an amount, never an account number
    if isinstance(obj, Mapping):
        return {k: mask_payload(v, _key=str(k)) for k, v in obj.items()}
    if isinstance(obj, (str, bytes)):
        s = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
        if sensitive:
            # Mask every digit run, then any remaining alnum token of length > 4.
            return mask_text(s) if _DIGIT_RUN_RE.search(s) else _mask_short_tokens(s)
        return mask_text(s)
    if isinstance(obj, Sequence):
        return [mask_payload(v, _key=_key) for v in obj]
    if sensitive and isinstance(obj, int) and not isinstance(obj, bool):
        return mask_number(obj)
    return obj


def _mask_short_tokens(s: str) -> str:
    # Under a sensitive key, any token that contains 5+ digits is masked.
    return re.sub(r"\b[\w-]*\d[\w-]*\b", lambda m: _mask_if_numeric(m.group(0)), s)


def _mask_if_numeric(tok: str) -> str:
    digits = sum(c.isdigit() for c in tok)
    return (mask_number(tok) or "") if digits >= 5 else tok
