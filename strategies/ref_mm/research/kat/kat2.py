"""KAT-2: favorite-longshot pattern on the paper's observation set (same sample and return
definitions as KAT-1, docs/refs/makers_and_takers_2025.pdf section 3.3, Figure 5).

Pass: average post-fee return per observation rises with the 10-cent price band. Concretely,
Spearman rank correlation between band index and band mean return > 0 over bands with at least
200 observations, the 90c-99c band mean exceeds the 1c-10c band mean, and the 1c-10c band mean
is negative. The paper reports large losses below 10c and small positive returns above 50c.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from pmcore.ledger.runs import record_run
from strategies.ref_mm.research.kat.kat1 import (
    DEFAULT_PARAMS,
    eligible_markets,
    observations,
    returns,
)

BANDS: list[tuple[int, int, str]] = [
    (1, 1000, "1c-10c"),
    (1001, 2000, "11c-20c"),
    (2001, 3000, "21c-30c"),
    (3001, 4000, "31c-40c"),
    (4001, 4999, "41c-49c"),
    (5000, 5999, "50c-59c"),
    (6000, 6999, "60c-69c"),
    (7000, 7999, "70c-79c"),
    (8000, 8999, "80c-89c"),
    (9000, 9999, "90c-99c"),
]


def band_of(price_ticks: int) -> str | None:
    for lo, hi, name in BANDS:
        if lo <= price_ticks <= hi:
            return name
    return None


def spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for rank, i in enumerate(order):
            r[i] = float(rank)
        return r

    rx, ry = ranks(xs), ranks(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry, strict=True))
    return 1 - 6 * d2 / (n * (n * n - 1))


def run(params: dict[str, Any], min_obs: int = 200) -> dict[str, Any]:
    markets = eligible_markets(params)
    if not markets:
        return {"status": "NO-DATA"}
    obs = observations(markets, int(params["lookback_days"]))
    if obs.height == 0:
        return {"status": "NO-DATA"}
    r = returns(obs, Decimal(str(params["fee_rate"])), int(params["fee_lot_contracts"]))
    r = r.with_columns(
        pl.col("price_ticks").map_elements(band_of, return_dtype=pl.Utf8).alias("band")
    ).drop_nulls("band")
    table = []
    for _lo, _hi, name in BANDS:
        sub = r.filter(pl.col("band") == name)
        if sub.height == 0:
            continue
        table.append(
            {
                "band": name,
                "n": sub.height,
                "mean_pre_pct": 100 * float(sub["r_pre"].mean()),
                "mean_post_pct": 100 * float(sub["r_post"].mean()),
                "win_rate": float(sub["won"].mean()),
            }
        )
    usable = [t for t in table if t["n"] >= min_obs]
    if len(usable) < 3:
        return {"status": "NO-DATA", "table": table}
    rho = spearman(list(range(len(usable))), [t["mean_post_pct"] for t in usable])
    first, last = usable[0], usable[-1]
    passed = (
        rho > 0 and last["mean_post_pct"] > first["mean_post_pct"] and first["mean_post_pct"] < 0
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "spearman": rho,
        "table": table,
        "observations": int(r.height),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="research/kat1_params.json")
    args = ap.parse_args()
    p = Path(args.config)
    params = {**DEFAULT_PARAMS, **(json.loads(p.read_text()) if p.exists() else {})}
    out = run(params)
    record_run(
        "KAT-2",
        params,
        (params["window_start"], params["window_end"]),
        {k: v for k, v in out.items() if k != "table"} | {"table": out.get("table")},
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
