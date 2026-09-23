from __future__ import annotations

from decimal import Decimal

import pytest

from pmcore.venues.kalshi import normalize as kn
from pmcore.venues.polymarket import normalize as pn


def test_kalshi_trade_cents_fields() -> None:
    t = kn.normalize_trade(
        {
            "trade_id": "t1",
            "ticker": "KX-A",
            "count": 50,
            "yes_price": 44,
            "no_price": 56,
            "taker_side": "no",
            "created_time": "2025-03-01T12:00:00Z",
        }
    )
    assert t.yes_price_ticks == 4400 and t.no_price_ticks == 5600
    assert t.maker_side == "yes" and t.maker_paid_ticks == 4400
    assert t.count == Decimal(50)
    assert t.as_row()["date"] == "2025-03-01"


def test_kalshi_trade_dollars_preferred() -> None:
    t = kn.normalize_trade(
        {
            "trade_id": "t2",
            "ticker": "KX-A",
            "count": 7,
            "count_fp": "7.50",
            "yes_price": 62,
            "yes_price_dollars": "0.6250",
            "no_price_dollars": "0.3750",
            "taker_side": "yes",
            "created_time": "2025-03-01T12:00:00.123Z",
        }
    )
    assert t.yes_price_ticks == 6250 and t.maker_side == "no" and t.maker_paid_ticks == 3750
    assert t.count == Decimal("7.50") and t.as_row()["count_x100"] == 750


def test_kalshi_trade_inconsistent_refused() -> None:
    with pytest.raises(kn.NormalizeError):
        kn.normalize_trade(
            {
                "trade_id": "x",
                "ticker": "K",
                "count": 1,
                "yes_price": 44,
                "no_price": 55,
                "taker_side": "yes",
                "created_time": "2025-01-01T00:00:00Z",
            }
        )
    with pytest.raises(kn.NormalizeError):
        kn.normalize_trade(
            {
                "trade_id": "x",
                "ticker": "K",
                "count": 1,
                "yes_price": 44,
                "taker_side": "maybe",
                "created_time": "2025-01-01T00:00:00Z",
            }
        )


def test_kalshi_candle_row() -> None:
    r = kn.candle_row(
        "KX-A",
        {
            "end_period_ts": 1735689600,
            "yes_bid": {"open": 40, "high": 42, "low": 39, "close": 41},
            "yes_ask": {"open": 45, "high": 46, "low": 44, "close": 45},
            "price": {"open": None, "close": 43},
            "volume": 12,
            "open_interest": 100,
        },
    )
    assert (
        r["yes_bid_close_ticks"] == 4100
        and r["yes_ask_close_ticks"] == 4500
        and r["price_open_ticks"] is None
    )
    assert r["date"] == "2025-01-01" and r["volume_x100"] == 1200


def test_gamma_market_labels_not_index() -> None:
    m = pn.parse_gamma_market(
        {
            "id": "1",
            "conditionId": "0xc",
            "question": "Lakers vs Celtics",
            "outcomes": '["Celtics", "Lakers"]',
            "clobTokenIds": '["111", "222"]',
            "closed": True,
            "endDate": "2025-01-01T00:00:00Z",
        }
    )
    assert m.token_for_outcome("lakers") == "222"
    with pytest.raises(pn.NormalizeError):
        m.token_for_outcome("Yes")


def test_poly_print_conversion_to_yes_terms() -> None:
    raw = {
        "side": "BUY",
        "asset": "222",
        "price": "0.52",
        "size": "10",
        "timestamp": 1735689600,
        "conditionId": "0xc",
        "transactionHash": "0xabc",
    }
    p = pn.normalize_data_api_trade(raw, reference_token_id="111", other_token_id="222")
    # taker bought the other token at 0.52 -> taker sold YES at 0.48
    assert p.taker_dir == "sell" and p.yes_price_ticks == 4800 and not p.on_reference_token
    q = pn.normalize_data_api_trade({**raw, "asset": "111"}, "111", "222")
    assert q.taker_dir == "buy" and q.yes_price_ticks == 5200 and q.on_reference_token
    with pytest.raises(pn.NormalizeError):
        pn.normalize_data_api_trade({**raw, "asset": "999"}, "111", "222")
