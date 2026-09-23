"""Tier 0 per-fill quantities exactly as PREREGISTRATION section 2 defines them (audit finding
C9 asked for permanent tests pinned to the worked examples; see tests/test_prereg_examples.py).

All prices in ticks (1 cent = 100 ticks); FV in half-ticks (fv_x2); fees in ticks per contract
as Decimal. Returns are Decimal ticks per contract.

Worked example A: yes_price 4400, taker no -> maker YES at 4400. fv_x2 at t - L_ref = 9800
(0.49): edge = 4900 - 4400 = +500 ticks. fv_x2 at t + 1h = 9400: markout = 4700 - 4400 - fee.
Settlement YES: 10000 - 4400 - fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pmcore.money import MAX_PRICE_TICKS

Side = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class MakerFill:
    yes_price_ticks: int
    taker_side: Side

    @property
    def maker_side(self) -> Side:
        return "no" if self.taker_side == "yes" else "yes"

    @property
    def maker_paid_ticks(self) -> int:
        return (
            self.yes_price_ticks
            if self.maker_side == "yes"
            else MAX_PRICE_TICKS - self.yes_price_ticks
        )


def fv_ticks(fv_x2: int) -> Decimal:
    return Decimal(fv_x2) / 2


def edge_ticks(fill: MakerFill, fv_x2_ref: int) -> Decimal:
    """YES maker: FV - yes_price. NO maker: yes_price - FV."""
    fv = fv_ticks(fv_x2_ref)
    return (
        fv - fill.yes_price_ticks
        if fill.maker_side == "yes"
        else Decimal(fill.yes_price_ticks) - fv
    )


def markout_ticks(fill: MakerFill, fv_x2_h: int, fee_ticks: Decimal) -> Decimal:
    """YES maker: FV(t + h) - yes_price - fee. NO maker: yes_price - FV(t + h) - fee."""
    fv = fv_ticks(fv_x2_h)
    gross = (
        fv - fill.yes_price_ticks
        if fill.maker_side == "yes"
        else Decimal(fill.yes_price_ticks) - fv
    )
    return gross - fee_ticks


def settlement_ticks(fill: MakerFill, outcome_yes: bool, fee_ticks: Decimal) -> Decimal:
    """YES maker: outcome - yes_price - fee. NO maker: (1 - outcome) - (1 - yes_price) - fee."""
    o = MAX_PRICE_TICKS if outcome_yes else 0
    if fill.maker_side == "yes":
        return Decimal(o - fill.yes_price_ticks) - fee_ticks
    return Decimal((MAX_PRICE_TICKS - o) - (MAX_PRICE_TICKS - fill.yes_price_ticks)) - fee_ticks
