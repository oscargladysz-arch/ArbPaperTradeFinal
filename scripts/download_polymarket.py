#!/usr/bin/env python3
"""Resumable Polymarket Global read-only downloader (rules 10 and 12). Background usage:

    nohup .venv/bin/python scripts/download_polymarket.py --phase all > data/checkpoints/polymarket/log.txt 2>&1 &

Phases: markets (Gamma closed markets whose endDate falls in the window, paged by narrow
end-date windows to stay under any offset cap, ASSUMPTIONS P-API-1), trades (Data API prints per
condition id; TruncatedError marks the market for on-chain backfill, P-API-2/3).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from pmcore.config import load_settings
from pmcore.data import holdout
from pmcore.data.checkpoint import (
    DoneSet,
    Progress,
    append_jsonl,
    checkpoint_dir,
    read_jsonl,
    save_observed,
)
from pmcore.lake.parquet import write_partition
from pmcore.venues.http import HttpError
from pmcore.venues.polymarket.data_api import DataApi, TruncatedError
from pmcore.venues.polymarket.gamma import Gamma
from pmcore.venues.polymarket.normalize import (
    NormalizeError,
    normalize_data_api_trade,
    parse_gamma_market,
)

log = logging.getLogger("download_polymarket")


def phase_markets(
    g: Gamma, ck: Any, start: datetime, end: datetime, window_days: int, limit: int | None
) -> None:
    out = ck / "markets.jsonl"
    seen = {m["market_id"] for m in read_jsonl(out)}
    windows_done = DoneSet(ck / "market_windows_done.txt")
    t = start
    n = 0
    while t < end:
        t2 = min(t + timedelta(days=window_days), end)
        key = f"{t.date()}_{t2.date()}"
        if key in windows_done:
            t = t2
            continue
        pages = 0
        for page in g.closed_markets(
            end_date_min=t.date().isoformat(), end_date_max=t2.date().isoformat()
        ):
            pages += 1
            if page and not seen:
                save_observed("polymarket_gamma_closed_markets_page", page[:2])
            for raw in page:
                try:
                    m = parse_gamma_market(raw)
                except NormalizeError as e:
                    log.warning("gamma market %s: %s", raw.get("id"), e)
                    continue
                if m.market_id in seen:
                    continue
                append_jsonl(out, m.as_row())
                seen.add(m.market_id)
                n += 1
        append_jsonl(
            ck / "market_windows.jsonl", {"window": key, "pages": pages, "markets_total": len(seen)}
        )
        windows_done.add(key)
        if limit and n >= limit:
            break
        t = t2
    log.info("markets phase: %d markets on file", len(seen))


def phase_trades(d: DataApi, ck: Any, limit: int | None) -> None:
    markets = read_jsonl(ck / "markets.jsonl")
    done = DoneSet(ck / "trades_done.txt")
    prog = Progress(ck / "progress.json")
    todo = [m for m in markets if m["condition_id"] and m["market_id"] not in done]
    if limit:
        todo = todo[:limit]
    prog.update(phase="trades", total=len(markets), done=len(done))
    for i, m in enumerate(todo, 1):
        tokens = json.loads(m["token_ids"])
        if len(tokens) != 2:
            append_jsonl(
                ck / "trades_summary.jsonl",
                {"market_id": m["market_id"], "skip": f"{len(tokens)} tokens"},
            )
            done.add(m["market_id"])
            continue
        # Reference token is index 0 ONLY as a storage convention for raw prints; the mapper
        # re-labels sides from outcome labels. Both tokens' prints are stored with token_id.
        ref, other = tokens[0], tokens[1]
        rows = []
        truncated = False
        bad = 0
        try:
            for page in d.trades(m["condition_id"]):
                if page and not rows:
                    save_observed("polymarket_data_api_trades_page", page[:3])
                for raw in page:
                    try:
                        rows.append(normalize_data_api_trade(raw, ref, other))
                    except NormalizeError as e:
                        bad += 1
                        if bad <= 3:
                            log.warning("%s: %s", m["market_id"], e)
        except TruncatedError as e:
            truncated = True
            append_jsonl(
                ck / "truncated.jsonl",
                {"market_id": m["market_id"], "condition_id": e.condition_id, "offset": e.offset},
            )
        except HttpError as e:
            log.warning("%s trades failed: %s", m["market_id"], e)
            append_jsonl(
                ck / "trades_errors.jsonl", {"market_id": m["market_id"], "error": str(e)[:200]}
            )
            continue
        kept = holdout.filter_rows(rows, lambda p: p.ts, f"polymarket_trades:{m['market_id']}")
        if kept:
            df = pl.DataFrame([p.as_row() for p in kept])
            for (date,), part in df.group_by(["date"]):
                write_partition(part, "polymarket", "trades", str(date), m["condition_id"])
        # Time-filter observation (P-API-3): record min/max timestamp actually returned.
        ts_min = min((p.ts for p in kept), default=None)
        ts_max = max((p.ts for p in kept), default=None)
        append_jsonl(
            ck / "trades_summary.jsonl",
            {
                "market_id": m["market_id"],
                "n": len(kept),
                "bad": bad,
                "truncated": truncated,
                "ts_min": ts_min,
                "ts_max": ts_max,
            },
        )
        done.add(m["market_id"])
        if i % 10 == 0 or i == len(todo):
            prog.update(done=len(done), last_market=m["market_id"], last_n=len(kept))
    log.info("trades phase done: %d markets", len(done))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["markets", "trades", "all"], default="all")
    ap.add_argument("--start", default=holdout.IN_SAMPLE_START.date().isoformat())
    ap.add_argument("--end", default=None)
    ap.add_argument("--window-days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--rps", type=float, default=4.0)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    s = load_settings()
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end
        else holdout.in_sample_end_exclusive()
    )
    end = min(end, holdout.in_sample_end_exclusive())
    ck = checkpoint_dir("polymarket")
    g = Gamma(s.polymarket_gamma_base, ca_bundle=s.ca_bundle, rps=args.rps)
    d = DataApi(s.polymarket_data_api_base, ca_bundle=s.ca_bundle, rps=args.rps)
    try:
        if args.phase in ("markets", "all"):
            phase_markets(g, ck, start, end, args.window_days, args.limit)
        if args.phase in ("trades", "all"):
            phase_trades(d, ck, args.limit)
    finally:
        g.close()
        d.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
