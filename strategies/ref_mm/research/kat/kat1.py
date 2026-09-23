"""KAT-1: replicate Bürgi, Deng and Whelan (2025), "Makers and Takers" headline averages.

Pass: takers about -32% and makers about -10%, each within 3 percentage points, makers > takers.

The paper's exact sample window, contract universe, and return definition are ASSUMPTIONS
KAT-1 (OPEN until the PDF is read). This script therefore takes them as a config and REFUSES to
report PASS unless `paper_params_verified` is true in that config. Until then it reports the
numbers under the stated parameters and the status "UNVERIFIED-PARAMS".

Usage: .venv/bin/python -m strategies.ref_mm.research.kat.kat1 --config research/kat1_params.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from pmcore.data.checkpoint import checkpoint_dir, read_jsonl
from pmcore.lake.parquet import scan
from pmcore.ledger.runs import record_run
from strategies.ref_mm.research.kat.common import trade_returns, weighted_average

DEFAULT_PARAMS: dict[str, Any] = {
    "paper_params_verified": False,
    "window_start": "2024-01-01",
    "window_end": "2025-06-30",
    "universe": "all settled binary markets with a yes/no result",
    "return_basis": "per_dollar_staked",
    "include_fees": True,
    "weighting": "contract",
    "expected_taker_pct": -32.0,
    "expected_maker_pct": -10.0,
    "tolerance_pct": 3.0,
    "notes": "Fill from the paper (docs/refs/) before claiming PASS.",
}


def load_results() -> dict[str, tuple[bool, str]]:
    """ticker -> (yes_won, event_ticker) from the markets checkpoint."""
    out: dict[str, tuple[bool, str]] = {}
    for m in read_jsonl(checkpoint_dir("kalshi") / "markets.jsonl"):
        res = str(m.get("result") or "").lower()
        if res in ("yes", "no"):
            out[m["ticker"]] = (res == "yes", str(m.get("event_ticker") or ""))
    return out


def run(
    params: dict[str, Any], fee_multipliers: dict[str, tuple[Decimal, Decimal]] | None = None
) -> dict[str, Any]:
    results = load_results()
    start = datetime.fromisoformat(params["window_start"]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(params["window_end"]).replace(tzinfo=UTC)
    lf = scan("kalshi", "trades").filter(
        (pl.col("ts_us") >= int(start.timestamp() * 1e6))
        & (pl.col("ts_us") < int(end.timestamp() * 1e6))
    )
    df = lf.select(["ticker", "count_x100", "yes_price_ticks", "taker_side"]).collect()
    taker_pairs: list[tuple[Decimal, Decimal]] = []
    maker_pairs: list[tuple[Decimal, Decimal]] = []
    n_trades = 0
    n_unresolved = 0
    for row in df.iter_rows(named=True):
        r = results.get(row["ticker"])
        if r is None:
            n_unresolved += 1
            continue
        yes_won, event = r
        series = row["ticker"].split("-")[0]
        tm, mm = (fee_multipliers or {}).get(series, (Decimal(1), Decimal(0)))
        count = Decimal(row["count_x100"]) / 100
        tr = trade_returns(
            row["ticker"],
            event,
            count,
            row["yes_price_ticks"],
            row["taker_side"],
            yes_won,
            tm,
            mm,
            bool(params["include_fees"]),
        )
        taker_pairs.append((tr.taker_return, count))
        maker_pairs.append((tr.maker_return, count))
        n_trades += 1
    if not taker_pairs:
        return {"status": "NO-DATA", "n_trades": 0, "n_unresolved": n_unresolved}
    taker_pct = float(weighted_average(taker_pairs) * 100)
    maker_pct = float(weighted_average(maker_pairs) * 100)
    tol = float(params["tolerance_pct"])
    within = (
        abs(taker_pct - float(params["expected_taker_pct"])) <= tol
        and abs(maker_pct - float(params["expected_maker_pct"])) <= tol
    )
    ordered = maker_pct > taker_pct
    if not params.get("paper_params_verified"):
        status = "UNVERIFIED-PARAMS"
    else:
        status = "PASS" if (within and ordered) else "FAIL"
    return {
        "status": status,
        "taker_pct": taker_pct,
        "maker_pct": maker_pct,
        "within_tolerance": within,
        "makers_gt_takers": ordered,
        "n_trades": n_trades,
        "n_unresolved": n_unresolved,
        "params": params,
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
        {k: v for k, v in out.items() if k != "params"},
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
