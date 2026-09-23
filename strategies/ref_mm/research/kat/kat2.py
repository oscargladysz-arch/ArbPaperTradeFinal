"""KAT-2: favorite-longshot pattern. Average return per dollar staked must rise with the
contract price bucket (buyer's paid price), for takers and for makers pooled.

Pass: Spearman rank correlation between bucket index and bucket mean return > 0 across
buckets with at least 200 trades, and the top bucket mean > the bottom bucket mean.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from pmcore.lake.parquet import scan
from pmcore.ledger.runs import record_run
from strategies.ref_mm.research.kat.common import price_bucket, trade_returns, weighted_average
from strategies.ref_mm.research.kat.kat1 import DEFAULT_PARAMS, load_results


def run(params: dict[str, Any], min_trades: int = 200) -> dict[str, Any]:
    results = load_results()
    start = datetime.fromisoformat(params["window_start"]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(params["window_end"]).replace(tzinfo=UTC)
    df = (
        scan("kalshi", "trades")
        .filter(
            (pl.col("ts_us") >= int(start.timestamp() * 1e6))
            & (pl.col("ts_us") < int(end.timestamp() * 1e6))
        )
        .select(["ticker", "count_x100", "yes_price_ticks", "taker_side"])
        .collect()
    )
    buckets: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for row in df.iter_rows(named=True):
        r = results.get(row["ticker"])
        if r is None:
            continue
        yes_won, event = r
        count = Decimal(row["count_x100"]) / 100
        tr = trade_returns(
            row["ticker"],
            event,
            count,
            row["yes_price_ticks"],
            row["taker_side"],
            yes_won,
            Decimal(1),
            Decimal(0),
            bool(params["include_fees"]),
        )
        # Both sides of every trade are buyers of a contract at their paid price.
        buckets.setdefault(price_bucket(tr.taker_paid_ticks), []).append((tr.taker_return, count))
        buckets.setdefault(price_bucket(tr.maker_paid_ticks), []).append((tr.maker_return, count))
    table = []
    for b in sorted(buckets):
        pairs = buckets[b]
        table.append(
            {"bucket": b, "n": len(pairs), "mean_return_pct": float(weighted_average(pairs) * 100)}
        )
    usable = [t for t in table if t["n"] >= min_trades]
    if len(usable) < 3:
        return {"status": "NO-DATA", "table": table}
    ranks_x = list(range(len(usable)))
    ys = [t["mean_return_pct"] for t in usable]
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    ranks_y = [0] * len(ys)
    for rank, i in enumerate(order):
        ranks_y[i] = rank
    n = len(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(ranks_x, ranks_y, strict=True))
    spearman = 1 - 6 * d2 / (n * (n * n - 1))
    passed = spearman > 0 and usable[-1]["mean_return_pct"] > usable[0]["mean_return_pct"]
    return {"status": "PASS" if passed else "FAIL", "spearman": spearman, "table": table}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="research/kat1_params.json")
    args = ap.parse_args()
    from pathlib import Path

    p = Path(args.config)
    params = {**DEFAULT_PARAMS, **(json.loads(p.read_text()) if p.exists() else {})}
    out = run(params)
    record_run(
        "KAT-2",
        params,
        (params["window_start"], params["window_end"]),
        {k: v for k, v in out.items() if k != "table"},
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
