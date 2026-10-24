"""Money helpers. All invoice amounts are integer cents (CLAUDE.md invariant 6).

Never construct amounts from floats. Parse strings with :func:`parse_money_to_cents` and
format with :func:`format_cents`. Percentages and rates are expressed in basis points (bps,
1 bp = 0.01 %) so that tolerance math stays in integers.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator, StrictInt

__all__ = [
    "Cents",
    "MoneyParseError",
    "add",
    "apply_bps",
    "cents_from_decimal",
    "ensure_cents",
    "format_cents",
    "parse_money_to_cents",
    "pct_bps_of",
    "within_tolerance",
]


class MoneyParseError(ValueError):
    """Raised when a string cannot be parsed into an exact cent amount."""


def ensure_cents(value: object) -> int:
    """Return ``value`` if it is a plain ``int`` (not bool/float); raise ``TypeError`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"money must be integer cents, got {type(value).__name__}: {value!r}")
    return value


def _validate_cents(v: int) -> int:
    return ensure_cents(v)


#: Pydantic field type for integer cents. StrictInt rejects floats and numeric strings.
Cents = Annotated[StrictInt, AfterValidator(_validate_cents)]

_MONEY_RE = re.compile(
    r"""^\s*
    (?P<neg1>[-(])?\s*
    (?P<cur>US\$|USD|\$)?\s*
    (?P<neg2>-)?\s*
    (?P<num>\d{1,3}(?:,\d{3})+|\d+)?
    (?:\.(?P<frac>\d{1,2}))?
    \s*(?P<cur2>USD)?\s*
    (?P<close>\))?\s*$""",
    re.VERBOSE,
)


def parse_money_to_cents(text: str) -> int:
    """Parse a printed money string such as ``"$1,240.00"``, ``"1240"``, ``"(35.50)"``.

    Exact: never goes through float. Rejects more than 2 decimal places, empty strings and
    anything that is not unambiguously a USD amount.
    """
    if not isinstance(text, str):
        raise MoneyParseError(f"expected str, got {type(text).__name__}")
    m = _MONEY_RE.match(text)
    if not m or (m.group("num") is None and m.group("frac") is None):
        raise MoneyParseError(f"not a money amount: {text!r}")
    if (m.group("neg1") == "(") != bool(m.group("close")):
        raise MoneyParseError(f"unbalanced parentheses: {text!r}")
    whole = int((m.group("num") or "0").replace(",", ""))
    frac_s = m.group("frac") or "0"
    frac = int(frac_s.ljust(2, "0"))
    cents = whole * 100 + frac
    negative = m.group("neg1") in ("-", "(") or m.group("neg2") == "-"
    return -cents if negative else cents


def cents_from_decimal(value: Decimal, *, rounding: str = ROUND_HALF_UP) -> int:
    """Convert a dollar ``Decimal`` to cents, rounding half-up by default."""
    if not isinstance(value, Decimal):
        raise TypeError("cents_from_decimal requires a Decimal (never a float)")
    try:
        return int((value * 100).quantize(Decimal(1), rounding=rounding))
    except InvalidOperation as exc:  # pragma: no cover - NaN/inf
        raise MoneyParseError(str(exc)) from exc


def format_cents(cents: int, *, symbol: bool = True, thousands: bool = True) -> str:
    """Format integer cents as ``$1,240.00`` (negative as ``-$35.50``)."""
    ensure_cents(cents)
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    whole_s = f"{whole:,}" if thousands else str(whole)
    return f"{sign}{'$' if symbol else ''}{whole_s}.{frac:02d}"


def add(*amounts: int) -> int:
    """Sum integer cents, rejecting non-int inputs."""
    return sum(ensure_cents(a) for a in amounts)


def apply_bps(cents: int, bps: int) -> int:
    """Return ``cents * bps / 10_000`` rounded half-even to a whole cent (integer math)."""
    ensure_cents(cents)
    ensure_cents(bps)
    q = Decimal(cents) * Decimal(bps) / Decimal(10_000)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def pct_bps_of(part: int, whole: int) -> int:
    """Return ``part / whole`` in basis points (rounded half-even). ``whole`` must be non-zero."""
    ensure_cents(part)
    ensure_cents(whole)
    if whole == 0:
        raise ZeroDivisionError("pct_bps_of: whole is 0")
    q = Decimal(part) * Decimal(10_000) / Decimal(whole)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def within_tolerance(actual: int, expected: int, *, pct_bps: int, min_cents: int = 0) -> bool:
    """True if ``|actual - expected| <= max(expected * pct_bps / 10_000, min_cents)``.

    This is the R3 three-way-match tolerance shape: ``max(10%, $100)`` is
    ``pct_bps=1000, min_cents=10_000``.
    """
    ensure_cents(actual)
    ensure_cents(expected)
    allowed = max(abs(apply_bps(expected, pct_bps)), ensure_cents(min_cents))
    return abs(actual - expected) <= allowed
