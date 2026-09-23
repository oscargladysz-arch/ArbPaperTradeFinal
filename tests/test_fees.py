from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmcore.fees import kalshi as fees
from pmcore.money import price_ticks

ONE = Decimal(1)
ZERO = Decimal(0)


def test_spec_example_a_unrounded_per_contract() -> None:
    # 0.0175 * 0.44 * 0.56 = 0.004312 per contract, spec rounds to 0.0043
    raw = fees.fee_dollars_unrounded(1, price_ticks("0.44"), fees.MAKER_RATE, ONE)
    assert raw == Decimal("0.004312")


def test_spec_example_b_unrounded_per_contract() -> None:
    raw = fees.fee_dollars_unrounded(1, price_ticks("0.38"), fees.MAKER_RATE, ONE)
    assert raw == Decimal("0.004123")


def test_charged_fee_rounds_up_per_order() -> None:
    # 50 contracts at 0.44: 50 * 0.004312 = 0.2156 -> 22 cents
    assert fees.maker_fee_cents(50, price_ticks("0.44"), ONE) == 22
    # 1 contract at 0.44: 0.004312 -> 1 cent (this is the rounding cliff the spec must state)
    assert fees.maker_fee_cents(1, price_ticks("0.44"), ONE) == 1
    assert fees.fee_ticks_per_contract(22, 50) == Decimal("44")  # 0.0044 dollars


def test_taker_matches_legacy_engine_example() -> None:
    # legacy engine.py: ceil_to_cent(0.07 * C * P * (1 - P)); 100 contracts at 0.50 -> $1.75
    assert fees.taker_fee_cents(100, price_ticks("0.50")) == 175


def test_zero_multiplier_means_no_fee() -> None:
    assert fees.maker_fee_cents(1000, price_ticks("0.50"), ZERO) == 0


@given(
    st.integers(min_value=1, max_value=10_000),
    st.integers(min_value=0, max_value=10_000),
    st.sampled_from([ZERO, ONE, Decimal("0.5"), Decimal(2)]),
)
def test_ceiling_property(contracts: int, price: int, mult: Decimal) -> None:
    raw = fees.fee_dollars_unrounded(contracts, price, fees.MAKER_RATE, mult)
    cents = fees.maker_fee_cents(contracts, price, mult)
    charged = Decimal(cents) / 100
    assert charged >= raw
    assert charged - raw < Decimal("0.01")
    assert cents >= 0


def test_regime_lookup() -> None:
    s = fees.FeeSchedule(
        [
            fees.FeeRegime("KXNBA", fees.utc(2025, 1, 1), "quadratic", ONE, ZERO, "test"),
            fees.FeeRegime("KXNBA", fees.utc(2026, 7, 7), "quadratic", ONE, ONE, "test"),
        ]
    )
    before = fees.utc(2026, 7, 6)
    after = fees.utc(2026, 7, 8)
    assert s.maker_fee_cents("KXNBA", before, 50, price_ticks("0.44")) == 0
    assert s.maker_fee_cents("KXNBA", after, 50, price_ticks("0.44")) == 22
    with pytest.raises(fees.FeeError):
        s.regime_at("KXNBA", fees.utc(2024, 1, 1))
    with pytest.raises(fees.FeeError):
        s.regime_at("UNKNOWN", after)


def test_unknown_fee_type_refused() -> None:
    with pytest.raises(fees.FeeError):
        fees.FeeRegime("X", fees.utc(2025, 1, 1), "flat", ONE, ZERO, "test")


def test_fee_type_semantics() -> None:
    assert fees.maker_multiplier_from("quadratic", Decimal(1)) == 0
    assert fees.maker_multiplier_from("quadratic_with_maker_fees", Decimal(1)) == 1
    assert fees.maker_multiplier_from("quadratic_with_maker_fees", Decimal("0.5")) == Decimal("0.5")
    with pytest.raises(fees.FeeError):
        fees.maker_multiplier_from("margin_market_maker_program_fees", Decimal(1))
    r = fees.FeeRegime.from_api(
        "KXNBA", fees.utc(2026, 1, 1), "quadratic_with_maker_fees", "1", "test"
    )
    assert r.maker_multiplier == 1 and r.taker_multiplier == 1
