"""KAT-3: per candidate pair, correlation and median absolute gap between the Kalshi mid
(1-minute candle bid/ask closes) and FV (Polymarket prints in the pair's YES terms).
Negative correlation flags the pair for side-inversion review.

Output: data/kat3.csv with pair_id, n_minutes, corr, median_abs_gap_cents, flagged.
"""

from __future__ import annotations

import argparse
import csv
import json
from typing import Any

import numpy as np
import polars as pl

from pmcore.data.holdout import repo_root
from pmcore.lake.parquet import scan
from pmcore.ledger.runs import record_run
from strategies.ref_mm.research.fv import NO_FV, PrintSeries, fv_x2_at


def pair_series(
    kalshi_ticker: str, condition_id: str, reference_token_id: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (t_us, kalshi_mid_x2, fv_x2) aligned on candle end times; rows with no FV or no mid dropped."""
    c = (
        scan("kalshi", "candles_1m")
        .filter(pl.col("ticker") == kalshi_ticker)
        .select(["end_ts_us", "yes_bid_close_ticks", "yes_ask_close_ticks"])
        .drop_nulls()
        .sort("end_ts_us")
        .collect()
    )
    p = (
        scan("polymarket", "trades")
        .filter(pl.col("condition_id") == condition_id)
        .select(["ts_us", "yes_price_ticks", "taker_dir", "token_id", "on_reference_token"])
        .collect()
    )
    if c.height == 0 or p.height == 0:
        return np.array([], np.int64), np.array([], np.int64), np.array([], np.int64)
    # The lake stores prints in terms of token_ids[0]; re-express in terms of the pair's reference token.
    stored_ref_is_pair_ref = (
        bool(
            p.filter(pl.col("on_reference_token"))["token_id"].head(1).to_list()
            == [reference_token_id]
        )
        if p.filter(pl.col("on_reference_token")).height
        else True
    )
    price = p["yes_price_ticks"].to_numpy().astype(np.int64)
    d = np.where(p["taker_dir"].to_numpy() == "buy", 1, -1).astype(np.int64)
    if not stored_ref_is_pair_ref:
        price = 10000 - price
        d = -d
    ps = PrintSeries.from_arrays(p["ts_us"].to_numpy().astype(np.int64), price, d)
    t = c["end_ts_us"].to_numpy().astype(np.int64)
    fv = fv_x2_at(ps, t)
    mid = (c["yes_bid_close_ticks"].to_numpy() + c["yes_ask_close_ticks"].to_numpy()).astype(
        np.int64
    )
    ok = fv != NO_FV
    return t[ok], mid[ok], fv[ok]


def evaluate(pairs: list[dict[str, Any]], min_minutes: int = 30) -> list[dict[str, Any]]:
    rows = []
    for pr in pairs:
        t, mid, fv = pair_series(pr["kalshi_ticker"], pr["condition_id"], pr["reference_token_id"])
        n = int(t.size)
        if n < min_minutes:
            rows.append(
                {
                    **pr,
                    "n_minutes": n,
                    "corr": None,
                    "median_abs_gap_cents": None,
                    "flagged": False,
                    "note": "insufficient overlap",
                }
            )
            continue
        m = mid.astype(np.float64)
        f = fv.astype(np.float64)
        corr = float(np.corrcoef(m, f)[0, 1]) if m.std() > 0 and f.std() > 0 else 0.0
        gap = float(np.median(np.abs(m - f)) / 200.0)  # half-ticks to cents
        rows.append(
            {
                **pr,
                "n_minutes": n,
                "corr": corr,
                "median_abs_gap_cents": gap,
                "flagged": corr < 0,
                "note": "",
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="data/candidate_pairs.csv")
    ap.add_argument("--out", default="data/kat3.csv")
    args = ap.parse_args()
    root = repo_root()
    with (root / args.pairs).open() as f:
        pairs = [r for r in csv.DictReader(f)]
    rows = evaluate(pairs)
    fields = list(rows[0].keys()) if rows else ["kalshi_ticker"]
    with (root / args.out).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    flagged = sum(1 for r in rows if r["flagged"])
    record_run(
        "KAT-3",
        {"pairs": args.pairs},
        ("2024-01-01", "2026-06-22"),
        {"pairs": len(rows), "flagged": flagged},
    )
    print(json.dumps({"pairs": len(rows), "flagged": flagged}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
