"""Permanent tests pinned to the PREREGISTRATION worked examples (audit finding C9)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from pmcore.fees.kalshi import MAKER_RATE, fee_cents_charged, fee_ticks_per_contract
from strategies.ref_mm.research import fv
from strategies.ref_mm.research.tier0.markout import (
    MakerFill,
    edge_ticks,
    markout_ticks,
    settlement_ticks,
)

ONE = Decimal(1)


def test_example_a_yes_maker() -> None:
    f = MakerFill(4400, "no")
    assert f.maker_side == "yes" and f.maker_paid_ticks == 4400
    assert edge_ticks(f, 9800) == Decimal(500)  # 0.49 - 0.44 = +0.05
    fee = fee_ticks_per_contract(
        fee_cents_charged(50, 4400, MAKER_RATE, ONE), 50
    )  # 22 cents / 50 = 44 ticks
    assert fee == Decimal(44)
    assert markout_ticks(f, 9400, fee) == Decimal(256)  # 0.47 - 0.44 - 0.0044 = +0.0256
    assert settlement_ticks(f, True, fee) == Decimal(5556) and settlement_ticks(
        f, False, fee
    ) == Decimal(-4444)


def test_example_b_no_maker() -> None:
    f = MakerFill(6200, "yes")
    assert f.maker_side == "no" and f.maker_paid_ticks == 3800
    assert edge_ticks(f, 11600) == Decimal(400)  # 0.62 - 0.58 = +0.04
    fee = fee_ticks_per_contract(
        fee_cents_charged(1, 3800, MAKER_RATE, ONE), 1
    )  # 0.004123 -> 1 cent -> 100 ticks
    assert fee == Decimal(100)
    assert markout_ticks(f, 12000, fee) == Decimal(100)  # 0.62 - 0.60 - 0.01 = +0.01
    assert settlement_ticks(f, False, fee) == Decimal(6100)  # (1 - 0) - (1 - 0.62) - 0.01 = +0.61
    assert settlement_ticks(f, True, fee) == Decimal(-3900)  # (1 - 1) - (1 - 0.62) - 0.01 = -0.39


def test_fee_example_fractional_count_rounds_up() -> None:
    # 1.67 contracts at 0.44: 0.0175 * 1.67 * 0.44 * 0.56 = 0.00720 dollars -> 1 cent -> 100 / 1.67 ticks per contract
    c = fee_cents_charged(Decimal("1.67"), 4400, MAKER_RATE, ONE)
    assert c == 1
    assert fee_ticks_per_contract(c, Decimal("1.67")) == Decimal(100) / Decimal("1.67")


def test_fv_second_resolution_lag() -> None:
    # A print stamped at floor-second t - 2 could have happened at t - 1.000001: excluded (audit C5).
    t = 1_000 * 1_000_000
    ps = fv.PrintSeries.from_arrays(
        np.array([t - 2_000_000], np.int64), np.array([5000], np.int64), np.array([1], np.int64)
    )
    assert fv.fv_x2_at(ps, np.array([t], np.int64))[0] == fv.NO_FV
    assert (
        fv.fv_x2_at(ps, np.array([t + 1_000_000], np.int64))[0] == 10000
    )  # stamped t - 2 s, second ended by t - 1 s... needs t + 1 to clear lag


def test_side_alignment_sub_title_guard() -> None:
    from pmcore.venues.polymarket.normalize import parse_gamma_market
    from strategies.ref_mm.mapping.sides import align_sides

    m = parse_gamma_market(
        {
            "id": "1",
            "conditionId": "0xc",
            "question": "Will the Fed cut rates in March?",
            "outcomes": '["Yes","No"]',
            "clobTokenIds": '["111","222"]',
        }
    )
    a = align_sides(m, "Fed decision in March?", "No change", "")
    assert a.status in ("REJECT", "UNRESOLVED")
    b = align_sides(m, "Fed decision in March?", "Cut", "")
    assert b.status == "RESOLVED"
