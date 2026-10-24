"""Masking helpers: bank and account numbers are shown as last-4 only (CLAUDE.md invariant 9).

Use :func:`mask_number` for a single value, :func:`mask_text` to scrub free text (logs, trace
attributes, LLM-quoted spans shown in the UI) and :func:`mask_payload` for JSON-like
structures before they reach the audit log, logs, traces or anonymous UI responses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "SENSITIVE_KEY_RE",
    "last4",
    "mask_bank_text",
    "mask_number",
    "mask_payload",
    "mask_text",
]

MASK_CHAR = "•"

#: Keys whose values are always masked inside payloads.
SENSITIVE_KEY_RE = re.compile(
    r"(bank|account|acct|routing|aba|iban|swift|bic|remit_to_bank|sort_code|card)", re.IGNORECASE
)

# A run of 6+ digits, optionally separated by single spaces or dashes (account/routing numbers,
# IBAN bodies), wherever it sits: standalone, or glued to letters, punctuation or hyphens
# ("newaccoun000555067890", "acct:000555067890", "account-000555067890", "ACCT123456789"). OCR
# of hidden or faint text often drops the spaces, so a glued run is treated as an account
# number. The run must not continue a longer digit run or a decimal ("0.00012345",
# "3,1234567"), so amounts survive; ISO dates are exempted in :func:`_mask_match`.
_DIGIT_RUN = r"(?<!\d)(?<!\d[.,])\d(?:[ -]?\d){5,}(?!\d)"
_DIGIT_RUN_RE = re.compile(_DIGIT_RUN)
#: IBAN-shaped tokens: country code, check digits, then 11-30 alphanumerics, compact
#: (``GB82WEST12345698765432``) or printed in groups of four (``GB82 WEST 1234 5698 7654 32``).
_IBAN = (
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}"
    r"(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: (?=[A-Z0-9]{0,2}\d)[A-Z0-9]{1,3})?)"
    r"(?![A-Za-z0-9])"
)
#: Formats that are never account numbers and pass through unchanged: UUIDs, md5 / sha1 /
#: sha256 hex digests (document hashes, audit-chain hashes, trace ids) and ISO dates (also
#: when a time follows: ``2026-10-24 12:30``). Anything
#: else that merely contains hex (``deadbeef000555067890``) is still masked.
_EXEMPT = (
    r"(?<![0-9A-Za-z-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}(?![0-9A-Za-z-])"
    r"|(?<![0-9A-Za-z])(?:[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32}"
    r"|[0-9A-F]{64}|[0-9A-F]{40}|[0-9A-F]{32})(?![0-9A-Za-z])"
    r"|(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])"
)
_TEXT_RE = re.compile(f"(?P<exempt>{_EXEMPT})|(?P<iban>{_IBAN})|(?P<run>{_DIGIT_RUN})")
#: Keys whose string values are hashes or identifiers, not free text (exempt by key when the
#: value is a single id-shaped token; a sensitive key always wins).
_ID_KEY_RE = re.compile(
    r"(^id$|_id$|_ids$|^ids$|^sha256$|^phash$|^hash$|_hash$|^idempotency_key$|^requestid$"
    r"|^trace_?id$|^span_?id$)",
    re.IGNORECASE,
)
_ID_VALUE_RE = re.compile(r"^[A-Za-z0-9_:./-]{1,200}$")
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
    """Mask every long digit run (6+ digits, glued to letters or not) and every IBAN-shaped
    token in free text to its last 4. ISO dates, decimals, UUIDs and hex digests are kept."""
    if text is None:
        return None
    return _TEXT_RE.sub(_mask_match, text)


def mask_bank_text(text: str | None) -> str | None:
    """Mask free text known to hold bank details (a remit-to block): as :func:`mask_text`, then
    any remaining token with 5+ digits too."""
    if text is None:
        return None
    return str(mask_payload({"remit_to_bank_details": text})["remit_to_bank_details"])


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_CONTIGUOUS6_RE = re.compile(r"\d{6}")


def _mask_match(m: re.Match[str]) -> str:
    s = m.group(0)
    if m.group("exempt") is not None or _ISO_DATE_RE.match(s):
        return s  # ids, digests and ISO dates are not account numbers
    if m.group("run") is not None and _glued(m) and not _account_like(s):
        # A short grouped code glued to a word ("heron-26-7200", "SR-12-3456"): an invoice or
        # file reference, not the space-less OCR of an account number (those are contiguous).
        return s
    return mask_number(s) or ""


def _glued(m: re.Match[str]) -> bool:
    before = m.string[m.start() - 1] if m.start() > 0 else ""
    return before.isalpha() or before in "-_"


def _account_like(run: str) -> bool:
    """6+ contiguous digits, or 8+ digits in groups (``0005-5506-7890``)."""
    return bool(_CONTIGUOUS6_RE.search(run)) or sum(c.isdigit() for c in run) >= 8


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
    if (
        not sensitive
        and _key is not None
        and isinstance(obj, str)
        and _ID_KEY_RE.search(_key)
        and _ID_VALUE_RE.match(obj)
    ):
        return obj  # a hash or identifier field (sha256, phash, *_id, requestid)
    if isinstance(obj, Mapping):
        return {k: mask_payload(v, _key=str(k)) for k, v in obj.items()}
    if isinstance(obj, (str, bytes)):
        s = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
        if sensitive:
            # Mask every digit run, then any remaining alnum token of length > 4.
            return _mask_short_tokens(mask_text(s) or "")
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
