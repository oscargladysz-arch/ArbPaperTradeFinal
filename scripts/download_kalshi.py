#!/usr/bin/env python3
"""Resumable Kalshi public-data downloader (rule 12). Run in the background:

    nohup .venv/bin/python scripts/download_kalshi.py --phase all > data/checkpoints/kalshi/log.txt 2>&1 &
    cat data/checkpoints/kalshi/progress.json     # cheap progress check

Phases: fees (series list, fee changes), markets (settled markets in window, live + historical),
trades (public tape per market), candles (1-minute bars per market). Every phase is idempotent
and resumes from its checkpoint. Holdout rows are refused by pmcore.data.holdout.

Endpoint names and parameters are ASSUMPTIONS (K-API-*). The first raw response of each
endpoint is saved to docs/observed/ so the docs can be checked against observed behavior.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
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
from pmcore.venues.kalshi.normalize import (
    NormalizeError,
    candle_row,
    market_row,
    normalize_trade,
    parse_ts,
)
from pmcore.venues.kalshi.public import KalshiPublic

log = logging.getLogger("download_kalshi")
IN_SAMPLE_START = holdout.IN_SAMPLE_START


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def phase_fees(k: KalshiPublic, ck: Any) -> None:
    fc = k.series_fee_changes(show_historical=True)
    save_observed("kalshi_series_fee_changes", fc)
    (ck / "series_fee_changes.json").write_text(json.dumps(fc, indent=2, sort_keys=True))
    try:
        ec = k.events_fee_changes()
        save_observed("kalshi_events_fee_changes", ec)
        (ck / "events_fee_changes.json").write_text(json.dumps(ec, indent=2, sort_keys=True))
    except HttpError as e:
        log.warning("events/fee_changes failed (K-FEE-4): %s", e)
    series_path = ck / "series.jsonl"
    if not series_path.exists():
        data = k.list_series()
        save_observed("kalshi_series", data)
        for s in data.get("series", []) or []:
            append_jsonl(series_path, s)
    log.info("fees phase done")


def phase_markets(
    k: KalshiPublic, ck: Any, start: datetime, end: datetime, limit: int | None
) -> None:
    out = ck / "markets.jsonl"
    seen = {m["ticker"] for m in read_jsonl(out)}
    n = 0
    # Live window: settled markets with close in [start, end).
    for page in k.markets(status="settled", min_close_ts=_epoch(start), max_close_ts=_epoch(end)):
        if not seen and page:
            save_observed("kalshi_markets_settled_page", page[:3])
        for m in page:
            if m["ticker"] in seen:
                continue
            append_jsonl(out, market_row(m))
            seen.add(m["ticker"])
            n += 1
        if limit and n >= limit:
            break
    # Historical namespace (K-API-10). Filters unknown; page everything and filter locally.
    try:
        cutoff = k.historical_cutoff()
        save_observed("kalshi_historical_cutoff", cutoff)
        (ck / "historical_cutoff.json").write_text(json.dumps(cutoff, indent=2, sort_keys=True))
        for page in k.historical_markets():
            if page:
                save_observed("kalshi_historical_markets_page", page[:3])
            for m in page:
                if m["ticker"] in seen:
                    continue
                row = market_row(m)
                ct = row.get("close_time")
                if ct and not (start <= parse_ts(ct) < end):
                    continue
                append_jsonl(out, row)
                seen.add(m["ticker"])
                n += 1
            if limit and n >= limit:
                break
    except HttpError as e:
        log.warning("historical namespace failed (K-API-2/9/10): %s", e)
    log.info("markets phase: %d markets on file", len(seen))


def _trade_pages(k: KalshiPublic, ticker: str, min_ts: int, max_ts: int, use_hist: bool) -> Any:
    return (
        k.historical_trades(ticker=ticker, min_ts=min_ts, max_ts=max_ts)
        if use_hist
        else k.trades(ticker=ticker, min_ts=min_ts, max_ts=max_ts)
    )


def phase_trades(k: KalshiPublic, ck: Any, end: datetime, limit: int | None) -> None:
    markets = read_jsonl(ck / "markets.jsonl")
    done = DoneSet(ck / "trades_done.txt")
    prog = Progress(ck / "progress.json")
    cutoff_raw = (
        (ck / "historical_cutoff.json").read_text()
        if (ck / "historical_cutoff.json").exists()
        else ""
    )
    todo = [m for m in markets if m["ticker"] not in done]
    if limit:
        todo = todo[:limit]
    prog.update(phase="trades", total=len(markets), done=len(done))
    for i, m in enumerate(todo, 1):
        ticker = m["ticker"]
        open_t = parse_ts(m["open_time"]) if m.get("open_time") else IN_SAMPLE_START
        close_t = parse_ts(m["close_time"]) if m.get("close_time") else end
        min_ts, max_ts = (
            _epoch(max(open_t, IN_SAMPLE_START)),
            _epoch(min(close_t + timedelta(days=1), end)),
        )
        rows: list[dict[str, Any]] = []
        bad = 0
        use_hist = False
        for attempt in (False, True):
            try:
                for page in _trade_pages(k, ticker, min_ts, max_ts, attempt):
                    if page and not rows:
                        save_observed("kalshi_trades_page", page[:3])
                    for raw in page:
                        try:
                            rows.append(normalize_trade(raw))
                        except NormalizeError as e:
                            bad += 1
                            if bad <= 3:
                                log.warning("%s: %s", ticker, e)
                use_hist = attempt
                if rows or not attempt:
                    if rows:
                        break
            except HttpError as e:
                log.warning(
                    "%s trades via %s failed: %s", ticker, "historical" if attempt else "live", e
                )
                if attempt:
                    rows = []
        kept = holdout.filter_rows(rows, lambda t: t.ts, f"kalshi_trades:{ticker}")
        if kept:
            df = pl.DataFrame([t.as_row() for t in kept])
            for (date,), part in df.group_by(["date"]):
                write_partition(part, "kalshi", "trades", str(date), ticker)
        append_jsonl(
            ck / "trades_summary.jsonl",
            {
                "ticker": ticker,
                "n": len(kept),
                "bad": bad,
                "via": "historical" if use_hist else "live",
                "cutoff": cutoff_raw[:80],
            },
        )
        done.add(ticker)
        if i % 10 == 0 or i == len(todo):
            prog.update(done=len(done), last_ticker=ticker, last_n=len(kept))
    log.info("trades phase done: %d markets", len(done))


def phase_candles(
    k: KalshiPublic, ck: Any, end: datetime, limit: int | None, chunk_days: int
) -> None:
    markets = read_jsonl(ck / "markets.jsonl")
    done = DoneSet(ck / "candles_done.txt")
    prog = Progress(ck / "progress.json")
    todo = [m for m in markets if m["ticker"] not in done]
    if limit:
        todo = todo[:limit]
    prog.update(phase="candles", total=len(markets), done=len(done))
    for i, m in enumerate(todo, 1):
        ticker = m["ticker"]
        series = m.get("series_ticker") or (m.get("event_ticker") or "").rsplit("-", 1)[0]
        open_t = max(
            parse_ts(m["open_time"]) if m.get("open_time") else IN_SAMPLE_START, IN_SAMPLE_START
        )
        close_t = min(parse_ts(m["close_time"]) if m.get("close_time") else end, end)
        rows: list[dict[str, Any]] = []
        t = open_t
        failed = False
        while t < close_t:
            t2 = min(t + timedelta(days=chunk_days), close_t)
            data: Any = None
            for hist in (False, True):
                try:
                    data = (
                        k.historical_candlesticks(ticker, _epoch(t), _epoch(t2), 1)
                        if hist
                        else k.candlesticks(series, ticker, _epoch(t), _epoch(t2), 1)
                    )
                    break
                except HttpError as e:
                    if hist:
                        log.warning(
                            "%s candles %s..%s failed both ways: %s", ticker, t.date(), t2.date(), e
                        )
                        failed = True
            if data:
                if not rows:
                    save_observed(
                        "kalshi_candlesticks",
                        {"ticker": ticker, "sample": (data.get("candlesticks") or [])[:3]},
                    )
                for c in data.get("candlesticks") or []:
                    try:
                        rows.append(candle_row(ticker, c))
                    except NormalizeError as e:
                        log.warning("%s candle: %s", ticker, e)
            t = t2
        kept = holdout.filter_rows(
            rows,
            lambda r: datetime.fromtimestamp(r["end_ts_us"] / 1e6, tz=UTC),
            f"kalshi_candles:{ticker}",
        )
        if kept:
            df = pl.DataFrame(kept)
            for (date,), part in df.group_by(["date"]):
                write_partition(part, "kalshi", "candles_1m", str(date), ticker)
        append_jsonl(
            ck / "candles_summary.jsonl",
            {"ticker": ticker, "n": len(kept), "failed_chunks": failed},
        )
        done.add(ticker)
        if i % 10 == 0 or i == len(todo):
            prog.update(done=len(done), last_ticker=ticker, last_n=len(kept))
    log.info("candles phase done: %d markets", len(done))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase", choices=["fees", "markets", "trades", "candles", "all"], default="all"
    )
    ap.add_argument("--start", default=IN_SAMPLE_START.date().isoformat())
    ap.add_argument(
        "--end", default=None, help="exclusive; defaults to the holdout boundary while locked"
    )
    ap.add_argument("--limit", type=int, default=None, help="max markets per phase (smoke tests)")
    ap.add_argument("--chunk-days", type=int, default=7)
    ap.add_argument("--rps", type=float, default=5.0)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    settings = load_settings()
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end
        else holdout.in_sample_end_exclusive()
    )
    end = min(end, holdout.in_sample_end_exclusive())
    ck = checkpoint_dir("kalshi")
    k = KalshiPublic(
        settings.kalshi_rest_base,
        settings.kalshi_historical_base,
        ca_bundle=settings.ca_bundle,
        rps=args.rps,
    )
    t0 = time.time()
    try:
        if args.phase in ("fees", "all"):
            phase_fees(k, ck)
        if args.phase in ("markets", "all"):
            phase_markets(k, ck, start, end, args.limit)
        if args.phase in ("trades", "all"):
            phase_trades(k, ck, end, args.limit)
        if args.phase in ("candles", "all"):
            phase_candles(k, ck, end, args.limit, args.chunk_days)
    finally:
        k.close()
    log.info("finished in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
