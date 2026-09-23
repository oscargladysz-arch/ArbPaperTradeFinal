from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmcore import money


def test_spec_examples() -> None:
    assert money.ticks_from_decimal_dollars("0.44") == 4400
    assert money.complement(4400) == 5600
    assert money.to_decimal_dollars(5600) == Decimal("0.5600")
    assert money.ticks_from_cents(44) == 4400
    assert money.notional_ticks(4400, 50) == 220_000  # $22.00


def test_sub_penny_exact() -> None:
    assert money.ticks_from_decimal_dollars("0.4450") == 4450
    assert money.ticks_from_decimal_dollars("0.445") == 4450


def test_inexact_raises() -> None:
    with pytest.raises(money.MoneyError):
        money.ticks_from_decimal_dollars("0.44505")
    with pytest.raises(money.MoneyError):
        money.ticks_from_decimal_dollars("abc")


def test_price_range() -> None:
    with pytest.raises(money.MoneyError):
        money.price_ticks("1.0001")
    with pytest.raises(money.MoneyError):
        money.check_price(-1)


def test_float_rejected() -> None:
    with pytest.raises(money.MoneyError):
        money.check_price(4400.0)  # type: ignore[arg-type]
    with pytest.raises(money.MoneyError):
        money.ticks_from_cents(True)  # type: ignore[arg-type]


def test_round_to_tick() -> None:
    assert money.round_down_to_tick(5250, 100) == 5200
    assert money.round_up_to_tick(5250, 100) == 5300
    assert money.round_down_to_tick(5200, 100) == 5200


@given(st.integers(min_value=0, max_value=money.MAX_PRICE_TICKS))
def test_roundtrip_ticks_decimal(t: int) -> None:
    assert money.ticks_from_decimal_dollars(money.to_decimal_dollars(t)) == t
    assert money.complement(money.complement(t)) == t


@given(st.integers(min_value=0, max_value=money.MAX_PRICE_TICKS), st.sampled_from([1, 10, 50, 100]))
def test_round_down_never_above(t: int, tick: int) -> None:
    r = money.round_down_to_tick(t, tick)
    assert r <= t and r % tick == 0 and t - r < tick
