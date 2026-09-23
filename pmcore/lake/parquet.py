"""Parquet data lake partitioned by venue, dataset, date, and market; queried with DuckDB.

Layout: data/lake/venue=<venue>/dataset=<dataset>/date=<YYYY-MM-DD>/market=<market>/part-<n>.parquet
Prices are stored as integer ticks (see pmcore.money). Timestamps are UTC microseconds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from pmcore.data.holdout import repo_root

_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def lake_root() -> Path:
    return repo_root() / "data" / "lake"


def _safe(part: str) -> str:
    return _SAFE.sub("_", part)[:120] or "_"


def partition_dir(venue: str, dataset: str, date: str, market: str) -> Path:
    return (
        lake_root()
        / f"venue={_safe(venue)}"
        / f"dataset={_safe(dataset)}"
        / f"date={date}"
        / f"market={_safe(market)}"
    )


def write_partition(
    df: pl.DataFrame, venue: str, dataset: str, date: str, market: str, *, overwrite: bool = True
) -> Path:
    d = partition_dir(venue, dataset, date, market)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "part-0.parquet"
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    tmp = d / "part-0.parquet.tmp"
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(path)
    return path


def scan(venue: str, dataset: str) -> pl.LazyFrame:
    glob = str(
        lake_root() / f"venue={_safe(venue)}" / f"dataset={_safe(dataset)}" / "**" / "*.parquet"
    )
    return pl.scan_parquet(glob, hive_partitioning=True)


def query(sql: str, **views: tuple[str, str]) -> Any:
    """Run SQL with DuckDB. `views` maps a view name to (venue, dataset)."""
    con = duckdb.connect()
    for name, (venue, dataset) in views.items():
        glob = str(
            lake_root() / f"venue={_safe(venue)}" / f"dataset={_safe(dataset)}" / "**" / "*.parquet"
        )
        con.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}', hive_partitioning=true)"
        )
    return con.execute(sql).pl()
