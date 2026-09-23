"""KAT-1: replicate Bürgi, Deng and Whelan (2025), "Makers and Takers: The Economics of the
Kalshi Prediction Market" (docs/refs/makers_and_takers_2025.pdf), headline averages.

Method, taken from the paper (sections 3.1, 3.3, 5; ASSUMPTIONS KAT-1 VERIFIED 2026-09-23):
- Window: Kalshi's inception (2021) through April 2025.
- Universe: settled markets with total trading volume at close of at least $1,000, final
  bid-ask spread at most 20 cents, and open for at least 24 hours (hourly markets excluded).
  The paper reports 46,282 Yes contracts and 12,403 events after these filters.
- Observations: the last trade before market close, plus the last trade before the same
  time of day on each of the previous days up to 10 days out (at most 11 per market). Each
  trade yields two observations: the Yes contract at price p and the No contract at 1 - p.
  313,972 observations in the paper.
- Maker and taker: from the trade's taker side. The taker holds the taker-side contract, the
  maker the other one.
- Returns: pre-fee r = (y - p) / p. Post-fee r_c = (y - p - c) / (p + c), where c is the fee
  per contract for a 100-contract purchase at price p, 0.07 * 100 * p * (1 - p) rounded up to
  the cent, divided by 100. The same rate is applied to both sides (the paper predates maker
  fee schedules).
- Averages are equal-weighted across observations.
- Headline (paper): makers -11.99%, takers -31.46% post-fee. Also all contracts -20% pre-fee,
  -22% post-fee. Makers who bought at 50c or more: +2.6% pre-fee, +1.9% post-fee.
- Pass (spec): each within 3 percentage points of -32 and -10, with makers > takers.

Worked example: last trade yes_price 0.44, taker_side no. Taker holds NO at p = 0.56, maker
holds YES at p = 0.44. Market settles YES: taker pre-fee r = (0 - 0.56) / 0.56 = -1.0; maker
pre-fee r = (1 - 0.44) / 0.44 = +1.2727. Fee per contract at 0.44: ceil_cent(0.07 * 100 *
0.44 * 0.56) / 100 = ceil_cent(1.7248) / 100 = 1.73 / 100 = 0.0173. Maker post-fee
r_c = (1 - 0.44 - 0.0173) / (0.44 + 0.0173) = 0.5427 / 0.4573 = +1.1868.

Ambiguities and how they are resolved (recorded in research/kat1_params.json):
- "total trading volume ... of at least $1,000": Kalshi reports volume in contracts
  (volume_fp); $1 notional per contract is used, so volume_fp >= 1000. (KAT-1a)
- "final bid-ask spread": yes_ask_dollars - yes_bid_dollars on the archived market record,
  which is the book at close. (KAT-1b)
- "open at least 24 hours": close_time - open_time >= 24 hours. (KAT-1c)
- Block trades: kept, as the paper does not mention excluding them. (KAT-1d)
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import polars as pl

from pmcore.data.kalshi_markets import iter_markets
from pmcore.lake.parquet import scan
from pmcore.ledger.runs import record_run
from pmcore.money import MAX_PRICE_TICKS
from pmcore.venues.kalshi.normalize import parse_ts

DEFAULT_PARAMS: dict[str, Any] = {
    "paper_params_verified": True,
    "window_start": "2021-06-01",
    "window_end": "2025-05-01",
    "min_volume_contracts": 1000,
    "max_final_spread_ticks": 2000,
    "min_open_hours": 24,
    "lookback_days": 10,
    "fee_rate": "0.07",
    "fee_lot_contracts": 100,
    "expected_taker_pct": -31.46,
    "expected_maker_pct": -11.99,
    "tolerance_pct": 3.0,
    "weighting": "equal per observation",
}


def fee_per_contract_ticks(price_ticks: int, rate: Decimal, lot: int) -> Decimal:
    """ceil_cent(rate * lot * p * (1 - p)) / lot, in ticks (1 cent = 100 ticks)."""
    p = Decimal(price_ticks) / MAX_PRICE_TICKS
    raw_dollars = rate * lot * p * (1 - p)
    cents = (raw_dollars * 100).to_integral_value(rounding=ROUND_CEILING)
    return Decimal(cents) * 100 / lot


def eligible_markets(params: dict[str, Any]) -> list[dict[str, Any]]:
    start = datetime.fromisoformat(params["window_start"]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(params["window_end"]).replace(tzinfo=UTC)
    out = []
    for m in iter_markets():
        res = str(m.get("result") or "").lower()
        if res not in ("yes", "no") or not m.get("close_time") or not m.get("open_time"):
            continue
        close_t = parse_ts(m["close_time"])
        open_t = parse_ts(m["open_time"])
        if not (start <= close_t < end):
            continue
        if close_t - open_t < timedelta(hours=float(params["min_open_hours"])):
            continue
        try:
            vol = Decimal(str(m.get("volume_fp") or "0"))
            bid = Decimal(str(m.get("yes_bid_dollars") or "0"))
            ask = Decimal(str(m.get("yes_ask_dollars") or "1"))
        except ArithmeticError:
            continue
        if vol < params["min_volume_contracts"]:
            continue
        if (ask - bid) * MAX_PRICE_TICKS > params["max_final_spread_ticks"]:
            continue
        out.append(
            {
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker"),
                "close_us": int(close_t.timestamp() * 1e6),
                "yes_won": res == "yes",
            }
        )
    return out


def observations(markets: list[dict[str, Any]], lookback_days: int) -> pl.DataFrame:
    """One row per (market, day-offset, side) following the paper's sampling."""
    tickers = [m["ticker"] for m in markets]
    meta = pl.DataFrame(markets)
    trades = (
        scan("kalshi", "trades")
        .filter(pl.col("ticker").is_in(tickers))
        .select(["ticker", "ts_us", "yes_price_ticks", "taker_side"])
        .collect()
    )
    if trades.height == 0:
        return pl.DataFrame()
    df = trades.join(meta, on="ticker", how="inner").filter(pl.col("ts_us") <= pl.col("close_us"))
    # Last trade at or before close, and last trade at or before close - d days.
    last = (
        df.sort("ts_us")
        .group_by("ticker")
        .agg(pl.all().last())
        .with_columns(pl.lit(0).alias("day_offset"), pl.col("ts_us").alias("anchor_us"))
    )
    frames = [
        last.select(
            ["ticker", "event_ticker", "yes_price_ticks", "taker_side", "yes_won", "day_offset"]
        )
    ]
    anchors = last.select(["ticker", "anchor_us"])
    day_us = 86_400 * 1_000_000
    for d in range(1, lookback_days + 1):
        cut = df.join(anchors, on="ticker").filter(
            pl.col("ts_us") <= pl.col("anchor_us") - d * day_us
        )
        if cut.height == 0:
            continue
        lastd = (
            cut.sort("ts_us")
            .group_by("ticker")
            .agg(pl.all().last())
            .with_columns(pl.lit(d).alias("day_offset"))
        )
        frames.append(
            lastd.select(
                ["ticker", "event_ticker", "yes_price_ticks", "taker_side", "yes_won", "day_offset"]
            )
        )
    obs = pl.concat(frames)
    # Expand to two contracts per trade: the taker's contract and the maker's contract.
    taker = obs.with_columns(
        pl.when(pl.col("taker_side") == "yes")
        .then(pl.col("yes_price_ticks"))
        .otherwise(MAX_PRICE_TICKS - pl.col("yes_price_ticks"))
        .alias("price_ticks"),
        pl.when(pl.col("taker_side") == "yes")
        .then(pl.col("yes_won"))
        .otherwise(~pl.col("yes_won"))
        .alias("won"),
        pl.lit("taker").alias("role"),
    )
    maker = obs.with_columns(
        pl.when(pl.col("taker_side") == "yes")
        .then(MAX_PRICE_TICKS - pl.col("yes_price_ticks"))
        .otherwise(pl.col("yes_price_ticks"))
        .alias("price_ticks"),
        pl.when(pl.col("taker_side") == "yes")
        .then(~pl.col("yes_won"))
        .otherwise(pl.col("yes_won"))
        .alias("won"),
        pl.lit("maker").alias("role"),
    )
    return pl.concat([taker, maker]).select(
        ["ticker", "event_ticker", "day_offset", "role", "price_ticks", "won"]
    )


def returns(obs: pl.DataFrame, rate: Decimal, lot: int) -> pl.DataFrame:
    """Exact Decimal per-row returns; floats only in the final averages."""
    fee_by_price = {
        p: fee_per_contract_ticks(p, rate, lot) for p in obs["price_ticks"].unique().to_list()
    }
    pre, post = [], []
    for p, won in zip(obs["price_ticks"].to_list(), obs["won"].to_list(), strict=True):
        y = Decimal(MAX_PRICE_TICKS if won else 0)
        pd_ = Decimal(p)
        c = fee_by_price[p]
        pre.append(float((y - pd_) / pd_))
        post.append(float((y - pd_ - c) / (pd_ + c)))
    return obs.with_columns(pl.Series("r_pre", pre), pl.Series("r_post", post))


def run(params: dict[str, Any]) -> dict[str, Any]:
    markets = eligible_markets(params)
    if not markets:
        return {"status": "NO-DATA", "markets": 0}
    obs = observations(markets, int(params["lookback_days"]))
    if obs.height == 0:
        return {"status": "NO-DATA", "markets": len(markets), "observations": 0}
    r = returns(obs, Decimal(str(params["fee_rate"])), int(params["fee_lot_contracts"]))
    by_role = r.group_by("role").agg(
        pl.col("r_pre").mean(), pl.col("r_post").mean(), pl.len().alias("n")
    )
    stats = {row["role"]: row for row in by_role.iter_rows(named=True)}
    taker_pct = 100 * stats["taker"]["r_post"]
    maker_pct = 100 * stats["maker"]["r_post"]
    all_pre = 100 * float(r["r_pre"].mean())
    all_post = 100 * float(r["r_post"].mean())
    hi = r.filter((pl.col("role") == "maker") & (pl.col("price_ticks") >= 5000))
    tol = float(params["tolerance_pct"])
    within = (
        abs(taker_pct - float(params["expected_taker_pct"])) <= tol
        and abs(maker_pct - float(params["expected_maker_pct"])) <= tol
    )
    ordered = maker_pct > taker_pct
    status = (
        "PASS"
        if (within and ordered and params.get("paper_params_verified"))
        else ("FAIL" if params.get("paper_params_verified") else "UNVERIFIED-PARAMS")
    )
    return {
        "status": status,
        "markets": len(markets),
        "events": int(obs["event_ticker"].n_unique()),
        "observations": int(r.height),
        "taker_post_fee_pct": taker_pct,
        "maker_post_fee_pct": maker_pct,
        "taker_pre_fee_pct": 100 * stats["taker"]["r_pre"],
        "maker_pre_fee_pct": 100 * stats["maker"]["r_pre"],
        "all_pre_fee_pct": all_pre,
        "all_post_fee_pct": all_post,
        "maker_ge_50c_pre_pct": 100 * float(hi["r_pre"].mean()) if hi.height else None,
        "maker_ge_50c_post_pct": 100 * float(hi["r_post"].mean()) if hi.height else None,
        "within_tolerance": within,
        "makers_gt_takers": ordered,
        "paper": {
            "taker": -31.46,
            "maker": -11.99,
            "all_pre": -20.0,
            "all_post": -22.0,
            "observations": 313972,
            "yes_contracts": 46282,
            "events": 12403,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="research/kat1_params.json")
    args = ap.parse_args()
    p = Path(args.config)
    params = {**DEFAULT_PARAMS, **(json.loads(p.read_text()) if p.exists() else {})}
    out = run(params)
    record_run(
        "KAT-1",
        params,
        (params["window_start"], params["window_end"]),
        {k: v for k, v in out.items() if k != "paper"},
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
