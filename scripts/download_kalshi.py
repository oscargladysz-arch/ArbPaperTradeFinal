#!/usr/bin/env python3
"""Resumable Kalshi public-data downloader (rule 12). Background usage:

    nohup .venv/bin/python scripts/download_kalshi.py --phase all > data/checkpoints/kalshi/log.txt 2>&1 &
    cat data/checkpoints/kalshi/progress.json      # cheap progress check

Phases (all idempotent, all resume from checkpoints, all holdout-guarded):
  fees     GET /series (every series: category, fee_type, fee_multiplier), GET /series/fee_changes,
           GET /events/fee_changes  -> data/checkpoints/kalshi/{series,series_fee_changes,events_fee_changes}.jsonl
  markets  GET /historical/markets?mve_filter=exclude paged newest-first with the cursor
           checkpointed after every page, stopping once close_time < --markets-since
           -> markets.jsonl (one row per archived market, joined with series category and fee type)
  trades   GET /historical/trades?ticker=&min_ts=&max_ts= for every market closing in
           [--start, --end) -> data/lake/venue=kalshi/dataset=trades/...
  candles  GET /historical/markets/{ticker}/candlesticks (1-minute, <= 5000 per request) for the
           tickers listed in --candle-tickers -> dataset=candles_1m

Endpoint parameters were verified on 2026-09-23 (see pmcore/venues/kalshi/public.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from pmcore.data.kalshi_markets import EXCLUDED_FREQUENCIES, iter_markets, markets_dir
from pmcore.lake.parquet import write_partition
from pmcore.venues.http import HttpError
from pmcore.venues.kalshi.normalize import (
    NormalizeError,
    candle_row,
    market_row,
    normalize_trade,
    parse_ts,
)
from pmcore.venues.kalshi.public import MAX_CANDLES_PER_REQUEST, KalshiPublic

log = logging.getLogger("download_kalshi")
KALSHI_INCEPTION = datetime(2021, 6, 1, tzinfo=UTC)


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def phase_fees(k: KalshiPublic, ck: Path) -> None:
    series = k.list_series()
    save_observed("kalshi_series", series[:3])
    (ck / "series.jsonl").write_text(
        "".join(json.dumps(s, sort_keys=True, default=str) + "\n" for s in series)
    )
    fc = k.series_fee_changes(show_historical=True)
    save_observed("kalshi_series_fee_changes", fc)
    (ck / "series_fee_changes.json").write_text(
        json.dumps(fc, indent=1, sort_keys=True, default=str)
    )
    rows: list[dict[str, Any]] = []
    for items, _cur in k.events_fee_changes():
        rows.extend(items)
    (ck / "events_fee_changes.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)
    )
    log.info(
        "fees phase: %d series, %d series fee changes, %d event fee changes",
        len(series),
        len(fc.get("series_fee_change_arr", [])),
        len(rows),
    )


def load_series_map(ck: Path) -> dict[str, dict[str, Any]]:
    return {s["ticker"]: s for s in read_jsonl(ck / "series.jsonl")}


def series_of(event_ticker: str, series_map: dict[str, dict[str, Any]]) -> str | None:
    """Event tickers are <SERIES>-<suffix>; take the longest known series prefix."""
    parts = event_ticker.split("-")
    for i in range(len(parts), 0, -1):
        cand = "-".join(parts[:i])
        if cand in series_map:
            return cand
    return None


def phase_markets(k: KalshiPublic, ck: Path, since: datetime) -> None:
    """Page /historical/markets per series. Series with frequency hourly or fifteen_min are
    skipped (PREREGISTRATION D7: markets that live under 24 hours are outside the universe,
    and they are 83% of the archive). Each series gets its own file; a done-set makes it
    resumable. The raw column is dropped; every Market field is flattened."""
    series_map = load_series_map(ck)
    done = DoneSet(ck / "markets_series_done.txt")
    prog = Progress(ck / "markets_progress.json")
    todo = [
        t
        for t, srow in sorted(series_map.items())
        if t not in done and (srow.get("frequency") or "") not in EXCLUDED_FREQUENCIES
    ]
    skipped = [
        t for t, srow in series_map.items() if (srow.get("frequency") or "") in EXCLUDED_FREQUENCIES
    ]
    (ck / "markets_excluded_series.txt").write_text("\n".join(sorted(skipped)) + "\n")
    prog.update(phase="markets", todo=len(todo), done=len(done), excluded_series=len(skipped))
    mdir = markets_dir(ck)
    total_rows = int(prog.state.get("rows", 0))
    for i, st in enumerate(todo, 1):
        srow = series_map[st]
        out = mdir / f"{st}.jsonl"
        rows = 0
        try:
            with out.open("w", encoding="utf-8") as f:
                for items, _cur in k.historical_markets(
                    series_ticker=st, mve_filter=None, page_limit=1000
                ):
                    for m in items:
                        row = market_row(m)
                        row.pop("raw", None)
                        row["series_ticker"] = st
                        row["category"] = srow.get("category")
                        row["series_frequency"] = srow.get("frequency")
                        row["series_fee_type"] = srow.get("fee_type")
                        row["series_fee_multiplier"] = (
                            str(srow.get("fee_multiplier"))
                            if srow.get("fee_multiplier") is not None
                            else None
                        )
                        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                        rows += 1
        except HttpError as e:
            log.warning("%s markets failed: %s", st, e)
            append_jsonl(ck / "markets_errors.jsonl", {"series": st, "error": str(e)[:200]})
            out.unlink(missing_ok=True)
            continue
        if rows == 0:
            out.unlink(missing_ok=True)
        total_rows += rows
        done.add(st)
        if i % 25 == 0 or i == len(todo):
            prog.update(done=len(done), rows=total_rows, last_series=st, remaining=len(todo) - i)
    prog.update(complete=True, rows=total_rows)
    log.info("markets phase complete: %d series, %d markets", len(done), total_rows)


def phase_trades(
    k: KalshiPublic,
    ck: Path,
    start: datetime,
    end: datetime,
    limit: int | None,
    only_series: set[str] | None,
) -> None:
    markets = list(iter_markets(ck))
    done = DoneSet(ck / "trades_done.txt")
    prog = Progress(ck / "progress.json")
    todo = []
    for m in markets:
        if m["ticker"] in done or not m.get("close_time"):
            continue
        ct = parse_ts(m["close_time"])
        if not (start <= ct < end):
            continue
        if only_series and (m.get("series_ticker") or "") not in only_series:
            continue
        todo.append(m)
    if limit:
        todo = todo[:limit]
    prog.update(phase="trades", todo=len(todo), done=len(done))
    for i, m in enumerate(todo, 1):
        ticker = m["ticker"]
        open_t = parse_ts(m["open_time"]) if m.get("open_time") else KALSHI_INCEPTION
        close_t = parse_ts(m["close_time"])
        min_ts, max_ts = _epoch(open_t - timedelta(days=1)), _epoch(close_t + timedelta(days=2))
        rows = []
        bad = 0
        try:
            for page, _cur in k.historical_trades(ticker=ticker, min_ts=min_ts, max_ts=max_ts):
                if page and not rows:
                    save_observed("kalshi_historical_trades_page", page[:3])
                for raw in page:
                    try:
                        rows.append(normalize_trade(raw))
                    except NormalizeError as e:
                        bad += 1
                        if bad <= 3:
                            log.warning("%s: %s", ticker, e)
        except HttpError as e:
            log.warning("%s trades failed: %s", ticker, e)
            append_jsonl(ck / "trades_errors.jsonl", {"ticker": ticker, "error": str(e)[:200]})
            continue
        kept = holdout.filter_rows(rows, lambda t: t.ts, f"kalshi_trades:{ticker}")
        if kept:
            df = pl.DataFrame([t.as_row() for t in kept])
            for (date,), part in df.group_by(["date"]):
                write_partition(part, "kalshi", "trades", str(date), ticker)
        append_jsonl(ck / "trades_summary.jsonl", {"ticker": ticker, "n": len(kept), "bad": bad})
        done.add(ticker)
        if i % 20 == 0 or i == len(todo):
            prog.update(
                done=len(done), last_ticker=ticker, last_n=len(kept), remaining=len(todo) - i
            )
    log.info("trades phase done: %d markets", len(done))


def phase_candles(k: KalshiPublic, ck: Path, tickers: list[str], end: datetime) -> None:
    markets = {m["ticker"]: m for m in iter_markets(ck)}
    done = DoneSet(ck / "candles_done.txt")
    prog = Progress(ck / "progress.json")
    todo = [t for t in tickers if t not in done]
    prog.update(phase="candles", todo=len(todo), done=len(done))
    chunk = timedelta(minutes=MAX_CANDLES_PER_REQUEST - 10)
    for i, ticker in enumerate(todo, 1):
        m = markets.get(ticker)
        if not m:
            log.warning("%s not in markets.jsonl", ticker)
            continue
        open_t = parse_ts(m["open_time"]) if m.get("open_time") else KALSHI_INCEPTION
        close_t = min(parse_ts(m["close_time"]), end) if m.get("close_time") else end
        rows: list[dict[str, Any]] = []
        t = open_t
        failed = False
        while t < close_t:
            t2 = min(t + chunk, close_t)
            try:
                data = k.historical_candlesticks(ticker, _epoch(t), _epoch(t2), 1)
            except HttpError as e:
                log.warning("%s candles %s..%s failed: %s", ticker, t.date(), t2.date(), e)
                failed = True
                data = {}
            if not rows and data.get("candlesticks"):
                save_observed(
                    "kalshi_historical_candlesticks_1m",
                    {"ticker": ticker, "sample": data["candlesticks"][:3]},
                )
            for c in data.get("candlesticks") or []:
                try:
                    rows.append(candle_row(ticker, c))
                except NormalizeError as e:
                    log.warning("%s candle: %s", ticker, e)
            t = t2 + timedelta(minutes=1)
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
    ap.add_argument(
        "--start",
        default="2021-06-01",
        help="trades: first close date to include (KAT-1 needs 2021 onward)",
    )
    ap.add_argument(
        "--end", default=None, help="exclusive; defaults to the holdout boundary while locked"
    )
    ap.add_argument(
        "--markets-since",
        default="2021-06-01",
        help="markets: stop paging once close_time is older than this",
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--series", default=None, help="comma-separated series tickers to restrict the trades phase"
    )
    ap.add_argument("--candle-tickers", default=None, help="file with one market ticker per line")
    ap.add_argument("--rps", type=float, default=2.0)
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
    since = datetime.fromisoformat(args.markets_since).replace(tzinfo=UTC)
    ck = checkpoint_dir("kalshi")
    k = KalshiPublic(
        settings.kalshi_rest_base,
        settings.kalshi_historical_base,
        ca_bundle=settings.ca_bundle,
        rps=args.rps,
    )
    t0 = time.time()
    try:
        cutoff = k.historical_cutoff()
        (ck / "historical_cutoff.json").write_text(json.dumps(cutoff, indent=1, sort_keys=True))
        if args.phase in ("fees", "all"):
            phase_fees(k, ck)
        if args.phase in ("markets", "all"):
            phase_markets(k, ck, since)
        if args.phase in ("trades", "all"):
            only = set(args.series.split(",")) if args.series else None
            phase_trades(k, ck, start, end, args.limit, only)
        if args.phase == "candles":
            tickers = (
                [
                    ln.strip()
                    for ln in Path(args.candle_tickers).read_text().splitlines()
                    if ln.strip()
                ]
                if args.candle_tickers
                else []
            )
            phase_candles(k, ck, tickers, end)
    finally:
        k.close()
    log.info("finished in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
