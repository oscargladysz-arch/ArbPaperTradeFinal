"""Kalshi fee math, exact.

Published schedule (see docs/ASSUMPTIONS.md, K-FEE-1 .. K-FEE-4):
    taker fee = round_up_to_cent(M_taker * 0.07   * C * P * (1 - P))
    maker fee = round_up_to_cent(M_maker * 0.0175 * C * P * (1 - P))
where C is the number of contracts in the order, P the price in dollars, and M the series
multiplier in force that day (maker default 0, taker default 1; some sports series have
maker multiplier 1). Rounding is up to the next cent per order.

Worked example (spec example A, per contract, unrounded): 0.0175 * 0.44 * 0.56 = 0.004312
dollars. For a 50-contract fill the exact charged fee is round_up_to_cent(50 * 0.004312) =
round_up_to_cent(0.2156) = 0.22 dollars = 22 cents, which is 0.0044 dollars per contract.
For a 1-contract fill the exact charged fee is round_up_to_cent(0.004312) = 0.01 dollars, so
the per-contract cost is 0.01, more than double the unrounded 0.0043. Research must use the
exact charged fee (see docs/PREREGISTRATION.md, fee section).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Final

from pmcore.money import TICKS_PER_DOLLAR, check_price, to_decimal_dollars

TAKER_RATE: Final[Decimal] = Decimal("0.07")
MAKER_RATE: Final[Decimal] = Decimal("0.0175")
_CENT: Final[Decimal] = Decimal("0.01")
_ONE: Final[Decimal] = Decimal(1)


class FeeError(ValueError):
    pass


def fee_dollars_unrounded(
    contracts: int | Decimal, price: int, rate: Decimal, multiplier: Decimal
) -> Decimal:
    """multiplier * rate * C * P * (1 - P) in dollars, no rounding. Exact Decimal."""
    check_price(price)
    c = Decimal(contracts)
    if c < 0:
        raise FeeError(f"contracts must be non-negative, got {contracts}")
    p = to_decimal_dollars(price)
    return multiplier * rate * c * p * (_ONE - p)


def fee_cents_charged(
    contracts: int | Decimal, price: int, rate: Decimal, multiplier: Decimal
) -> int:
    """The fee Kalshi actually charges on one order: unrounded fee rounded UP to the next cent.

    Returns whole cents. 50 contracts at 0.44 with maker multiplier 1 -> 22 cents.
    """
    raw = fee_dollars_unrounded(contracts, price, rate, multiplier)
    cents = (raw / _CENT).to_integral_value(rounding=ROUND_CEILING)
    return int(cents)


def maker_fee_cents(contracts: int | Decimal, price: int, multiplier: Decimal) -> int:
    return fee_cents_charged(contracts, price, MAKER_RATE, multiplier)


def taker_fee_cents(contracts: int | Decimal, price: int, multiplier: Decimal = Decimal(1)) -> int:
    return fee_cents_charged(contracts, price, TAKER_RATE, multiplier)


def fee_ticks_per_contract(fee_cents: int, contracts: int | Decimal) -> Decimal:
    """Allocate a charged fee across the contracts of the order, in ticks, exact Decimal.

    22 cents over 50 contracts = 2200 ticks / 50 = 44 ticks per contract (0.0044 dollars).
    """
    c = Decimal(contracts)
    if c <= 0:
        raise FeeError("contracts must be positive to allocate a fee")
    return Decimal(fee_cents) * Decimal(TICKS_PER_DOLLAR // 100) / c


# Fee types observed on GET /series/fee_changes and GET /series (2026-09-23), see ASSUMPTIONS K-FEE-5.
# `quadratic`: takers pay fee_multiplier * 0.07 * C * P * (1 - P); makers pay nothing.
# `quadratic_with_maker_fees`: takers as above; makers pay fee_multiplier * 0.0175 * C * P * (1 - P)
#   (the maker rate is 25% of the taker rate, per the fee schedule and the Aug 2026 changelog).
# `quadratic_with_combo_maker_fees`: combo (MVE) markets, maker factor 50% instead of 25%; MVE
#   markets are excluded from this research, so the type is accepted but never priced.
# `flat` and `margin_market_maker_program_fees` (perps): not event-contract quadratic fees; refused.
MAKER_FACTOR_BY_FEE_TYPE: dict[str, Decimal] = {
    "quadratic": Decimal(0),
    "quadratic_with_maker_fees": Decimal(1),
    "quadratic_with_combo_maker_fees": Decimal(2),
}


def maker_multiplier_from(fee_type: str, fee_multiplier: Decimal) -> Decimal:
    """Spec M_maker: the series multiplier when the fee type charges makers, else 0."""
    if fee_type not in MAKER_FACTOR_BY_FEE_TYPE:
        raise FeeError(
            f"fee_type {fee_type!r} is not an event-contract quadratic schedule (K-FEE-5)"
        )
    return fee_multiplier * MAKER_FACTOR_BY_FEE_TYPE[fee_type]


@dataclass(frozen=True, slots=True)
class FeeRegime:
    """The fee parameters in force for one series from `effective_ts` onward."""

    series_ticker: str
    effective_ts: datetime
    fee_type: str
    taker_multiplier: Decimal
    maker_multiplier: Decimal
    source: str

    def __post_init__(self) -> None:
        if self.effective_ts.tzinfo is None:
            raise FeeError("effective_ts must be timezone-aware")
        if self.fee_type not in MAKER_FACTOR_BY_FEE_TYPE:
            raise FeeError(
                f"fee_type {self.fee_type!r} on {self.series_ticker} is not implemented; "
                "verify the schedule first (ASSUMPTIONS K-FEE-5)"
            )

    @classmethod
    def from_api(
        cls,
        series_ticker: str,
        effective_ts: datetime,
        fee_type: str,
        fee_multiplier: Decimal | str | int,
        source: str,
    ) -> FeeRegime:
        m = Decimal(str(fee_multiplier))
        return cls(
            series_ticker, effective_ts, fee_type, m, maker_multiplier_from(fee_type, m), source
        )


class FeeSchedule:
    """Per-series regimes ordered by effective time. `regime_at` picks the one in force."""

    def __init__(self, regimes: list[FeeRegime]) -> None:
        self._by_series: dict[str, list[FeeRegime]] = {}
        for r in regimes:
            self._by_series.setdefault(r.series_ticker, []).append(r)
        for lst in self._by_series.values():
            lst.sort(key=lambda r: r.effective_ts)

    def regime_at(self, series_ticker: str, ts: datetime) -> FeeRegime:
        if ts.tzinfo is None:
            raise FeeError("ts must be timezone-aware")
        regimes = self._by_series.get(series_ticker)
        if not regimes:
            raise FeeError(f"no fee regime known for series {series_ticker}")
        chosen: FeeRegime | None = None
        for r in regimes:
            if r.effective_ts <= ts:
                chosen = r
            else:
                break
        if chosen is None:
            raise FeeError(
                f"no fee regime for {series_ticker} in force at {ts.isoformat()}; earliest is "
                f"{regimes[0].effective_ts.isoformat()}"
            )
        return chosen

    def maker_fee_cents(self, series_ticker: str, ts: datetime, contracts: int, price: int) -> int:
        return maker_fee_cents(contracts, price, self.regime_at(series_ticker, ts).maker_multiplier)

    def taker_fee_cents(self, series_ticker: str, ts: datetime, contracts: int, price: int) -> int:
        return taker_fee_cents(contracts, price, self.regime_at(series_ticker, ts).taker_multiplier)


def utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)
