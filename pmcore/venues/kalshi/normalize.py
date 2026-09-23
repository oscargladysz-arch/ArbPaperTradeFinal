"""Normalize raw Kalshi API records into typed, integer-tick values.

Field names follow ASSUMPTIONS K-API-7 and K-API-14. Sub-penny `*_dollars` strings are
preferred over integer-cent fields when present. The raw JSON is always kept alongside.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pmcore.money import MAX_PRICE_TICKS, ticks_from_cents, ticks_from_decimal_dollars

Side = Literal["yes", "no"]


class NormalizeError(ValueError):
    pass


def parse_ts(value: Any) -> datetime:
    """ISO-8601 with Z or offset, or integer epoch seconds, to aware UTC datetime."""
    if isinstance(value, bool):
        raise NormalizeError(f"bad timestamp {value!r}")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value:
        s = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise NormalizeError(f"bad timestamp {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    raise NormalizeError(f"bad timestamp {value!r}")


def _price_ticks(raw: dict[str, Any], dollars_key: str, cents_key: str) -> int | None:
    if raw.get(dollars_key) not in (None, ""):
        return ticks_from_decimal_dollars(str(raw[dollars_key]))
    if raw.get(cents_key) not in (None, ""):
        v = raw[cents_key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise NormalizeError(f"{cents_key} must be int cents, got {v!r}")
        return ticks_from_cents(v)
    return None


def _count(raw: dict[str, Any]) -> Decimal:
    if raw.get("count_fp") not in (None, ""):
        try:
            return Decimal(str(raw["count_fp"]))
        except InvalidOperation as exc:
            raise NormalizeError(f"bad count_fp {raw['count_fp']!r}") from exc
    if raw.get("count") is not None:
        return Decimal(int(raw["count"]))
    raise NormalizeError("trade has no count")


@dataclass(frozen=True, slots=True)
class KalshiTrade:
    trade_id: str
    ticker: str
    ts: datetime
    count: Decimal
    yes_price_ticks: int
    no_price_ticks: int
    taker_side: Side
    maker_side: Side
    maker_paid_ticks: int
    raw: str

    def as_row(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "ticker": self.ticker,
            "ts_us": int(self.ts.timestamp() * 1_000_000),
            "date": self.ts.date().isoformat(),
            "count_x100": int(self.count * 100),
            "yes_price_ticks": self.yes_price_ticks,
            "no_price_ticks": self.no_price_ticks,
            "taker_side": self.taker_side,
            "maker_side": self.maker_side,
            "maker_paid_ticks": self.maker_paid_ticks,
            "raw": self.raw,
        }


def normalize_trade(raw: dict[str, Any]) -> KalshiTrade:
    taker = str(raw.get("taker_side", "")).lower()
    if taker not in ("yes", "no"):
        raise NormalizeError(f"taker_side must be yes or no, got {raw.get('taker_side')!r}")
    yes = _price_ticks(raw, "yes_price_dollars", "yes_price")
    no = _price_ticks(raw, "no_price_dollars", "no_price")
    if yes is None and no is None:
        raise NormalizeError("trade has neither yes_price nor no_price")
    if yes is None and no is not None:
        yes = MAX_PRICE_TICKS - no
    if no is None and yes is not None:
        no = MAX_PRICE_TICKS - yes
    assert yes is not None and no is not None
    if yes + no != MAX_PRICE_TICKS:
        # Observed behavior beats docs: refuse rather than guess (K-API-7).
        raise NormalizeError(
            f"yes_price + no_price != 1.00 for trade {raw.get('trade_id')}: {yes} + {no}"
        )
    if not (0 <= yes <= MAX_PRICE_TICKS):
        raise NormalizeError(f"yes price out of range: {yes}")
    taker_side: Side = "yes" if taker == "yes" else "no"
    maker_side: Side = "no" if taker_side == "yes" else "yes"
    maker_paid = yes if maker_side == "yes" else no
    return KalshiTrade(
        trade_id=str(raw.get("trade_id", "")),
        ticker=str(raw["ticker"]),
        ts=parse_ts(raw.get("created_time") or raw.get("ts")),
        count=_count(raw),
        yes_price_ticks=yes,
        no_price_ticks=no,
        taker_side=taker_side,
        maker_side=maker_side,
        maker_paid_ticks=maker_paid,
        raw=json.dumps(raw, sort_keys=True, separators=(",", ":")),
    )


MARKET_KEYS = (
    "ticker",
    "event_ticker",
    "series_ticker",
    "title",
    "subtitle",
    "yes_sub_title",
    "no_sub_title",
    "rules_primary",
    "rules_secondary",
    "category",
    "status",
    "result",
    "open_time",
    "close_time",
    "expiration_time",
    "expected_expiration_time",
    "settlement_ts",
    "settlement_value",
    "tick_size",
    "price_level_structure",
    "response_price_units",
    "market_type",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "custom_strike",
)


def market_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the fields mapping and settlement need; keep raw JSON (K-API-14)."""
    row: dict[str, Any] = {k: raw.get(k) for k in MARKET_KEYS}
    for k in (
        "open_time",
        "close_time",
        "expiration_time",
        "expected_expiration_time",
        "settlement_ts",
    ):
        v = row.get(k)
        row[k] = parse_ts(v).isoformat() if v not in (None, "") else None
    for k in ("floor_strike", "cap_strike", "custom_strike", "settlement_value", "tick_size"):
        v = row.get(k)
        row[k] = None if v is None else str(v)
    row["raw"] = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return row


def candle_row(ticker: str, raw: dict[str, Any]) -> dict[str, Any]:
    """1-minute candle (K-API-8). Prices in ticks; bid/ask carry no size."""

    def px(obj: Any, key: str) -> int | None:
        if not isinstance(obj, dict):
            return None
        v = obj.get(key + "_dollars", obj.get(key))
        if v in (None, ""):
            return None
        if isinstance(v, int) and not isinstance(v, bool) and key + "_dollars" not in obj:
            return ticks_from_cents(v)
        return ticks_from_decimal_dollars(str(v))

    end = raw.get("end_period_ts")
    ts = parse_ts(end)
    out: dict[str, Any] = {
        "ticker": ticker,
        "end_ts_us": int(ts.timestamp() * 1_000_000),
        "date": ts.date().isoformat(),
    }
    for group in ("yes_bid", "yes_ask", "price"):
        g = raw.get(group)
        for k in ("open", "high", "low", "close"):
            out[f"{group}_{k}_ticks"] = px(g, k)
    out["volume_x100"] = int(Decimal(str(raw.get("volume_fp", raw.get("volume", 0)) or 0)) * 100)
    out["open_interest_x100"] = int(
        Decimal(str(raw.get("open_interest_fp", raw.get("open_interest", 0)) or 0)) * 100
    )
    out["raw"] = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return out
