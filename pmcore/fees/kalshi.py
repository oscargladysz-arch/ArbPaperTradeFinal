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
    contracts: int, price: int, rate: Decimal, multiplier: Decimal
) -> Decimal:
    """multiplier * rate * C * P * (1 - P) in dollars, no rounding. Exact Decimal."""
    check_price(price)
    if contracts < 0:
        raise FeeError(f"contracts must be non-negative, got {contracts}")
    p = to_decimal_dollars(price)
    return multiplier * rate * Decimal(contracts) * p * (_ONE - p)


def fee_cents_charged(contracts: int, price: int, rate: Decimal, multiplier: Decimal) -> int:
    """The fee Kalshi actually charges on one order: unrounded fee rounded UP to the next cent.

    Returns whole cents. 50 contracts at 0.44 with maker multiplier 1 -> 22 cents.
    """
    raw = fee_dollars_unrounded(contracts, price, rate, multiplier)
    cents = (raw / _CENT).to_integral_value(rounding=ROUND_CEILING)
    return int(cents)


def maker_fee_cents(contracts: int, price: int, multiplier: Decimal) -> int:
    return fee_cents_charged(contracts, price, MAKER_RATE, multiplier)


def taker_fee_cents(contracts: int, price: int, multiplier: Decimal = Decimal(1)) -> int:
    return fee_cents_charged(contracts, price, TAKER_RATE, multiplier)


def fee_ticks_per_contract(fee_cents: int, contracts: int) -> Decimal:
    """Allocate a charged fee across the contracts of the order, in ticks, exact Decimal.

    22 cents over 50 contracts = 2200 ticks / 50 = 44 ticks per contract (0.0044 dollars).
    """
    if contracts <= 0:
        raise FeeError("contracts must be positive to allocate a fee")
    return Decimal(fee_cents) * Decimal(TICKS_PER_DOLLAR // 100) / Decimal(contracts)


@dataclass(frozen=True, slots=True)
class FeeRegime:
    """The fee parameters in force for one series from `effective_ts` onward.

    fee_type: "quadratic" is the P * (1 - P) schedule above. Any other value raises until it is
    verified and implemented (ASSUMPTIONS K-FEE-5).
    """

    series_ticker: str
    effective_ts: datetime
    fee_type: str
    taker_multiplier: Decimal
    maker_multiplier: Decimal
    source: str

    def __post_init__(self) -> None:
        if self.effective_ts.tzinfo is None:
            raise FeeError("effective_ts must be timezone-aware")
        if self.fee_type != "quadratic":
            raise FeeError(
                f"fee_type {self.fee_type!r} on {self.series_ticker} is not implemented; "
                "verify the schedule first (ASSUMPTIONS K-FEE-5)"
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
