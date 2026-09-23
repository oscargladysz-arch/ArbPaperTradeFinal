"""Shared return math for the known-answer tests (spec section 4).

Per-trade taker and maker returns from Kalshi public trades plus market results.
Prices in ticks (1 cent = 100 ticks), fees in ticks per contract (Decimal), outcome in {0, 1}
for YES. Everything money-side is exact; only the final averages are floats.

Worked example: trade yes_price 0.44 (4400 ticks), taker_side = no, market settles YES.
Taker bought NO at 5600 ticks; payoff 0; taker P&L = 0 - 5600 - taker_fee_ticks.
Maker bought YES at 4400; payoff 10000; maker P&L = 10000 - 4400 - maker_fee_ticks.
Return per dollar staked = P&L / price paid: taker = -5600/5600 = -100% (before fee),
maker = 5600/4400 = +127.3% (before fee).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmcore.fees.kalshi import MAKER_RATE, TAKER_RATE, fee_cents_charged, fee_ticks_per_contract
from pmcore.money import MAX_PRICE_TICKS


@dataclass(frozen=True, slots=True)
class TradeReturn:
    ticker: str
    event_ticker: str
    count: Decimal
    taker_side: str
    taker_paid_ticks: int
    maker_paid_ticks: int
    taker_pnl_ticks: Decimal
    maker_pnl_ticks: Decimal
    taker_return: Decimal  # per dollar staked
    maker_return: Decimal


def trade_returns(
    ticker: str,
    event_ticker: str,
    count: Decimal,
    yes_price_ticks: int,
    taker_side: str,
    yes_won: bool,
    taker_multiplier: Decimal,
    maker_multiplier: Decimal,
    include_fees: bool = True,
) -> TradeReturn:
    if taker_side not in ("yes", "no"):
        raise ValueError(taker_side)
    yes_payoff = MAX_PRICE_TICKS if yes_won else 0
    no_payoff = MAX_PRICE_TICKS - yes_payoff
    taker_paid = yes_price_ticks if taker_side == "yes" else MAX_PRICE_TICKS - yes_price_ticks
    maker_paid = MAX_PRICE_TICKS - taker_paid
    taker_payoff = yes_payoff if taker_side == "yes" else no_payoff
    maker_payoff = MAX_PRICE_TICKS - taker_payoff
    if include_fees:
        # Charged per order and rounded up to the cent, fractional counts included
        # (PREREGISTRATION D2; audit finding C10).
        tf = fee_ticks_per_contract(
            fee_cents_charged(count, taker_paid, TAKER_RATE, taker_multiplier), count
        )
        mf = fee_ticks_per_contract(
            fee_cents_charged(count, maker_paid, MAKER_RATE, maker_multiplier), count
        )
    else:
        tf = mf = Decimal(0)
    taker_pnl = Decimal(taker_payoff - taker_paid) - tf
    maker_pnl = Decimal(maker_payoff - maker_paid) - mf
    return TradeReturn(
        ticker=ticker,
        event_ticker=event_ticker,
        count=count,
        taker_side=taker_side,
        taker_paid_ticks=taker_paid,
        maker_paid_ticks=maker_paid,
        taker_pnl_ticks=taker_pnl,
        maker_pnl_ticks=maker_pnl,
        taker_return=taker_pnl / Decimal(taker_paid) if taker_paid else Decimal(0),
        maker_return=maker_pnl / Decimal(maker_paid) if maker_paid else Decimal(0),
    )


def weighted_average(pairs: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Contract-weighted average of (value, weight)."""
    w = sum((p[1] for p in pairs), Decimal(0))
    if w == 0:
        raise ValueError("zero total weight")
    return sum((p[0] * p[1] for p in pairs), Decimal(0)) / w


PRICE_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 1000),
    (1000, 2000),
    (2000, 3000),
    (3000, 4000),
    (4000, 5000),
    (5000, 6000),
    (6000, 7000),
    (7000, 8000),
    (8000, 9000),
    (9000, 10001),
)


def price_bucket(paid_ticks: int) -> str:
    for lo, hi in PRICE_BUCKETS:
        if lo <= paid_ticks < hi:
            return f"{lo / 10000:.1f}-{min(hi, 10000) / 10000:.1f}"
    raise ValueError(paid_ticks)
