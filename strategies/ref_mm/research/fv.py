"""Fair value from Polymarket prints (PREREGISTRATION 2.2), vectorized and exact.

FV(t) = midpoint of the latest taker-buy and latest taker-sell YES-terms prints with
t - L_ref - W < ts <= t - L_ref, W = 10 minutes, L_ref = 2 seconds. One side only -> that
print. Nothing -> no FV.

Units: prints in ticks (1 cent = 100 ticks). FV is returned in HALF-ticks as int64
(`fv_x2 = buy_ticks + sell_ticks`, or 2 * price when one side) so the midpoint stays exact.
fv_x2 = 9500 means 4750 ticks = 0.4750 dollars.

Worked example: latest taker buy (YES terms) 4800 at t - 30s, latest taker sell 4700 at
t - 90s. fv_x2 = 9500 -> FV = 0.4750. If the sell were 11 minutes old only the buy counts and
fv_x2 = 9600 -> FV = 0.4800.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

WINDOW_US = 10 * 60 * 1_000_000
L_REF_US = 2 * 1_000_000
NO_FV = np.int64(-1)


@dataclass(frozen=True, slots=True)
class PrintSeries:
    """Prints of one pair in YES terms, sorted by ts_us. taker_dir: 1 = buy, -1 = sell."""

    ts_us: NDArray[np.int64]
    price_ticks: NDArray[np.int64]
    taker_dir: NDArray[np.int64]

    @classmethod
    def from_arrays(
        cls, ts_us: NDArray[np.int64], price_ticks: NDArray[np.int64], taker_dir: NDArray[np.int64]
    ) -> PrintSeries:
        order = np.argsort(ts_us, kind="stable")
        return cls(
            np.asarray(ts_us, np.int64)[order],
            np.asarray(price_ticks, np.int64)[order],
            np.asarray(taker_dir, np.int64)[order],
        )


def _latest_before(
    ts_sorted: NDArray[np.int64], prices: NDArray[np.int64], t_us: NDArray[np.int64], window_us: int
) -> tuple[NDArray[np.int64], NDArray[np.bool_]]:
    if ts_sorted.size == 0:
        return np.zeros(t_us.shape, dtype=np.int64), np.zeros(t_us.shape, dtype=np.bool_)
    idx = np.searchsorted(ts_sorted, t_us, side="right") - 1
    ok = idx >= 0
    idx_safe = np.where(ok, idx, 0)
    age_ok = ok & ((t_us - ts_sorted[idx_safe]) < window_us)
    return prices[idx_safe], age_ok


def fv_x2_at(
    prints: PrintSeries,
    t_us: NDArray[np.int64],
    *,
    window_us: int = WINDOW_US,
    lag_us: int = L_REF_US,
) -> NDArray[np.int64]:
    """FV in half-ticks at each query time, or NO_FV (-1) where no print qualifies."""
    t_us = np.asarray(t_us, np.int64)
    cutoff = t_us - lag_us
    buys = prints.taker_dir > 0
    sells = prints.taker_dir < 0
    bp, bok = _latest_before(prints.ts_us[buys], prints.price_ticks[buys], cutoff, window_us)
    sp, sok = _latest_before(prints.ts_us[sells], prints.price_ticks[sells], cutoff, window_us)
    out = np.full(t_us.shape, NO_FV, dtype=np.int64)
    both = bok & sok
    out[both] = bp[both] + sp[both]
    only_b = bok & ~sok
    out[only_b] = 2 * bp[only_b]
    only_s = sok & ~bok
    out[only_s] = 2 * sp[only_s]
    return out


def fv_ticks_decimal_str(fv_x2: int) -> str:
    """Human display: half-ticks to dollars with 5 decimals. 9500 -> '0.47500'."""
    if fv_x2 < 0:
        return "none"
    return f"{fv_x2 / 20000:.5f}"
