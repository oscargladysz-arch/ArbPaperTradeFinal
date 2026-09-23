from __future__ import annotations

from pathlib import Path

import polars as pl

from pmcore.lake import parquet as lake


def test_write_and_scan(tmp_repo: Path) -> None:
    df = pl.DataFrame({"ts_us": [1, 2], "price_ticks": [4400, 5600], "count": [50, 10]})
    p = lake.write_partition(df, "kalshi", "trades", "2025-01-02", "KXTEST-25-A")
    assert p.exists()
    out = lake.scan("kalshi", "trades").collect()
    assert out.height == 2
    assert out["price_ticks"].dtype == pl.Int64
    q = lake.query("SELECT sum(count) AS n FROM t", t=("kalshi", "trades"))
    assert q["n"][0] == 60
