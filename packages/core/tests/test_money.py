from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from invoicedesk.money import (
    Cents,
    MoneyParseError,
    add,
    apply_bps,
    cents_from_decimal,
    ensure_cents,
    format_cents,
    parse_money_to_cents,
    pct_bps_of,
    within_tolerance,
)
from pydantic import BaseModel, ValidationError


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("$1,240.00", 124_000),
        ("1240", 124_000),
        ("1240.5", 124_050),
        ("$0.99", 99),
        (".75", 75),
        ("(35.50)", -3_550),
        ("-$12.00", -1_200),
        ("$-12.00", -1_200),
        ("USD 1,000,000.01", 100_000_001),
        ("  340.00 USD ", 34_000),
    ],
)
def test_parse(text: str, cents: int) -> None:
    assert parse_money_to_cents(text) == cents


@pytest.mark.parametrize("bad", ["", "abc", "1.234", "$", "(12.00", "1,23,4.00", "12.00)"])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money_to_cents(bad)


def test_parse_rejects_non_str() -> None:
    with pytest.raises(MoneyParseError):
        parse_money_to_cents(12.5)  # type: ignore[arg-type]


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_roundtrip(c: int) -> None:
    assert parse_money_to_cents(format_cents(c)) == c


def test_format() -> None:
    assert format_cents(124_000) == "$1,240.00"
    assert format_cents(-3_550) == "-$35.50"
    assert format_cents(5, symbol=False) == "0.05"
    assert format_cents(123_456_789, thousands=False) == "$1234567.89"


def test_ensure_cents_rejects_float_and_bool() -> None:
    with pytest.raises(TypeError):
        ensure_cents(1.0)
    with pytest.raises(TypeError):
        ensure_cents(True)
    with pytest.raises(TypeError):
        add(1, 2.5)  # type: ignore[arg-type]


def test_cents_field_is_strict() -> None:
    class M(BaseModel):
        amount: Cents

    assert M(amount=5).amount == 5
    with pytest.raises(ValidationError):
        M(amount=5.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        M(amount="5")  # type: ignore[arg-type]


def test_decimal_and_bps() -> None:
    assert cents_from_decimal(Decimal("12.345")) == 1235
    with pytest.raises(TypeError):
        cents_from_decimal(12.3)  # type: ignore[arg-type]
    assert apply_bps(100_000, 1000) == 10_000
    assert apply_bps(5, 5000) == 2  # 2.5 -> half-even -> 2
    assert pct_bps_of(34_000, 100_000) == 3400
    with pytest.raises(ZeroDivisionError):
        pct_bps_of(1, 0)
    assert add(1, 2, 3) == 6


def test_within_tolerance_r3_shape() -> None:
    # max(10 %, $100): estimate $500 -> allowed $100
    assert within_tolerance(60_000, 50_000, pct_bps=1000, min_cents=10_000)
    assert not within_tolerance(60_001, 50_000, pct_bps=1000, min_cents=10_000)
    # estimate $3,400 -> allowed $340
    assert within_tolerance(374_000, 340_000, pct_bps=1000, min_cents=10_000)
    assert not within_tolerance(374_001, 340_000, pct_bps=1000, min_cents=10_000)
