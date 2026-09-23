from __future__ import annotations

from decimal import Decimal

from strategies.ref_mm.research.kat.common import price_bucket, trade_returns, weighted_average


def test_worked_example_no_fees() -> None:
    tr = trade_returns(
        "K", "E", Decimal(50), 4400, "no", True, Decimal(1), Decimal(0), include_fees=False
    )
    assert tr.taker_paid_ticks == 5600 and tr.maker_paid_ticks == 4400
    assert tr.taker_pnl_ticks == Decimal(-5600) and tr.maker_pnl_ticks == Decimal(5600)
    assert tr.taker_return == Decimal(-1)
    assert abs(tr.maker_return - Decimal("1.2727")) < Decimal("0.001")


def test_fees_reduce_pnl() -> None:
    a = trade_returns(
        "K", "E", Decimal(50), 4400, "no", True, Decimal(1), Decimal(1), include_fees=True
    )
    b = trade_returns(
        "K", "E", Decimal(50), 4400, "no", True, Decimal(1), Decimal(1), include_fees=False
    )
    # taker: 0.07 * 50 * 0.56 * 0.44 = 0.8624 -> 87 cents -> 174 ticks per contract
    assert b.taker_pnl_ticks - a.taker_pnl_ticks == Decimal(174)
    # maker: 0.0175 * 50 * 0.44 * 0.56 = 0.2156 -> 22 cents -> 44 ticks per contract
    assert b.maker_pnl_ticks - a.maker_pnl_ticks == Decimal(44)


def test_weighted_average_and_buckets() -> None:
    assert weighted_average(
        [(Decimal(2), Decimal(100)), (Decimal(-1), Decimal(50)), (Decimal(4), Decimal(50))]
    ) == Decimal("1.75")
    assert (
        price_bucket(4400) == "0.4-0.5"
        and price_bucket(10000) == "0.9-1.0"
        and price_bucket(0) == "0.0-0.1"
    )
