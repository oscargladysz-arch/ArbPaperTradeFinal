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
  trades   one newest-first sweep of GET /historical/trades?min_ts=&max_ts= in 1-day windows,
           keeping trades whose ticker is in the kept market set -> data/lake/venue=kalshi/dataset=trades/...
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


def series_of(event_ticker: str, series_map: dict[str, dict[str, Any]]) -> tuple[str | None, str]:
    """Series for an event ticker. Event tickers are <SERIES>-<suffix>. Legacy (pre-2025) tickers
    lack the KX prefix (HIGHNY-24JUN02 belongs to today's KXHIGHNY), so the KX-prefixed form is
    tried too (OBSERVED 2026-09-23: series_ticker=KXHIGHNY does not return HIGHNY-* markets).
    Returns (series_ticker or None, method)."""
    parts = event_ticker.split("-")
    for i in range(len(parts), 0, -1):
        cand = "-".join(parts[:i])
        if cand in series_map:
            return cand, "exact"
        if not cand.startswith("KX") and ("KX" + cand) in series_map:
            return "KX" + cand, "kx_prefix"
    return None, "none"


def scheduled_end(row: dict[str, Any]) -> datetime | None:
    """The ex-ante scheduled end of a market: expected_expiration_time, else the legacy
    expiration_time, never close_time (which Kalshi moves earlier once an outcome is known;
    audit finding C1). Returns None when neither is present."""
    for k in ("expected_expiration_time", "expiration_time"):
        v = row.get(k)
        if v:
            try:
                return parse_ts(v)
            except NormalizeError:
                continue
    return None


def keep_market(row: dict[str, Any], srow: dict[str, Any] | None) -> bool:
    """PREREGISTRATION D7: drop markets scheduled to live under 24 hours unless their series is
    identified and is not hourly or fifteen_min (same-day sports markets stay). Lifetime uses the
    scheduled end, not the actual close (audit C1)."""
    try:
        end_t = scheduled_end(row) or parse_ts(row["close_time"])
        lifetime_h = (end_t - parse_ts(row["open_time"])).total_seconds() / 3600
    except (KeyError, TypeError, NormalizeError):
        return True
    if lifetime_h >= 24:
        return True
    if srow is None:
        return False
    return (srow.get("frequency") or "") not in EXCLUDED_FREQUENCIES


def phase_markets(k: KalshiPublic, ck: Path, since: datetime) -> None:
    """Page the whole archive with mve_filter=exclude alone (series_ticker only returns KX-era
    tickers; without mve_filter the archive is 99% multivariate combo markets; mve_filter cannot
    be combined with other filters). The cursor is checkpointed
    after every page; rows are filtered in flight by keep_market and written without the raw
    column to markets/archive-<chunk>.jsonl. Stops once a page's oldest close_time < since."""
    series_map = load_series_map(ck)
    prog = Progress(ck / "markets_progress.json")
    if prog.state.get("complete"):
        log.info("markets phase already complete (%d rows kept)", prog.state.get("kept", 0))
        return
    mdir = markets_dir(ck)
    cursor = prog.state.get("cursor")
    pages = int(prog.state.get("pages", 0))
    kept = int(prog.state.get("kept", 0))
    seen = int(prog.state.get("seen", 0))
    unmapped = int(prog.state.get("unmapped_series", 0))
    refused = int(prog.state.get("holdout_refused", 0))
    for items, next_cursor in k.historical_markets(
        mve_filter="exclude", page_limit=1000, start_cursor=cursor
    ):
        if pages == 0 and items:
            save_observed("kalshi_historical_markets_page", items[:3])
        chunk = mdir / f"archive-{pages // 100:05d}.jsonl"
        oldest: datetime | None = None
        with chunk.open("a", encoding="utf-8") as f:
            for m in items:
                seen += 1
                row = market_row(m)
                row.pop("raw", None)
                ct = row.get("close_time")
                dt = parse_ts(ct) if ct else None
                if dt is not None and (oldest is None or dt < oldest):
                    oldest = dt
                st, how = series_of(row.get("event_ticker") or "", series_map)
                srow = series_map.get(st) if st else None
                if srow is None:
                    unmapped += 1
                if m.get("mve_selected_legs") or not keep_market(row, srow):
                    continue
                sched = scheduled_end(row)
                settle = parse_ts(row["settlement_ts"]) if row.get("settlement_ts") else None
                stamp = (
                    max(x for x in (sched, settle, dt) if x is not None)
                    if any(x is not None for x in (sched, settle, dt))
                    else None
                )
                if stamp is not None and holdout.is_locked() and stamp >= holdout.HOLDOUT_START:
                    refused += 1
                    continue
                row["series_ticker"] = st
                row["series_match"] = how
                row["category"] = (srow or {}).get("category")
                row["series_frequency"] = (srow or {}).get("frequency")
                row["series_fee_type"] = (srow or {}).get("fee_type")
                row["series_fee_multiplier"] = (
                    str((srow or {}).get("fee_multiplier"))
                    if srow and srow.get("fee_multiplier") is not None
                    else None
                )
                f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                kept += 1
        pages += 1
        prog.update(
            cursor=next_cursor,
            pages=pages,
            seen=seen,
            kept=kept,
            unmapped_series=unmapped,
            holdout_refused=refused,
            oldest_close=oldest.isoformat() if oldest else None,
        )
        if (oldest is not None and oldest < since) or next_cursor is None:
            prog.update(complete=True)
            if refused:
                holdout.log_refusal("kalshi_markets", holdout.HOLDOUT_START, refused)
            log.info(
                "markets phase complete: %d pages, %d seen, %d kept, oldest close %s",
                pages,
                seen,
                kept,
                oldest,
            )
            return


def phase_trades(
    k: KalshiPublic,
    ck: Path,
    start: datetime,
    end: datetime,
    limit: int | None,
    only_series: set[str] | None,
) -> None:
    """Global sweep of GET /historical/trades by time window, newest-first, keeping only trades
    on tickers in the kept market set (markets/archive-*.jsonl). One pass over the tape costs
    ~1 request per 1,000 trades regardless of how many markets are in scope, which beats one
    request per market for a universe of tens of thousands of markets. Windows of --trades-window-days
    are checkpointed (cursor per window) so the sweep resumes exactly where it stopped."""
    kept = {
        m["ticker"]
        for m in iter_markets(ck)
        if not only_series or (m.get("series_ticker") or "") in only_series
    }
    if not kept:
        log.warning("no kept markets on file; run the markets phase first")
        return
    prog = Progress(ck / "trades_progress.json")
    done = DoneSet(ck / "trades_windows_done.txt")
    window = timedelta(days=int(prog.state.get("window_days", 1)))
    t_hi = end
    n_seen = int(prog.state.get("seen", 0))
    n_kept = int(prog.state.get("kept", 0))
    prog.update(phase="trades", kept_tickers=len(kept), window_days=window.days)
    buf: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def flush() -> None:
        for (date, ticker), rows in buf.items():
            df = pl.DataFrame(rows)
            write_partition(df, "kalshi", "trades", date, ticker)
        buf.clear()

    windows_done = 0
    while t_hi > start:
        t_lo = max(t_hi - window, start)
        key = f"{t_lo.date()}_{t_hi.date()}"
        if key in done:
            t_hi = t_lo
            continue
        cursor = prog.state.get("cursor") if prog.state.get("window") == key else None
        bad = 0
        pages = 0
        try:
            for page, next_cursor in k._paginate(
                k.hist,
                "/historical/trades",
                {"min_ts": _epoch(t_lo), "max_ts": _epoch(t_hi)},
                "trades",
                1000,
                cursor,
            ):
                pages += 1
                for raw in page:
                    n_seen += 1
                    if raw.get("ticker") not in kept:
                        continue
                    try:
                        t = normalize_trade(raw)
                    except NormalizeError as e:
                        bad += 1
                        if bad <= 3:
                            log.warning("%s: %s", raw.get("ticker"), e)
                        continue
                    if holdout.is_locked() and t.ts >= holdout.HOLDOUT_START:
                        continue
                    row = t.as_row()
                    buf.setdefault((row["date"], t.ticker), []).append(row)
                    n_kept += 1
                if pages % 20 == 0:
                    prog.update(window=key, cursor=next_cursor, seen=n_seen, kept=n_kept)
        except HttpError as e:
            log.warning("window %s failed: %s", key, e)
            append_jsonl(ck / "trades_errors.jsonl", {"window": key, "error": str(e)[:200]})
        flush()
        done.add(key)
        windows_done += 1
        prog.update(
            window=key,
            cursor=None,
            seen=n_seen,
            kept=n_kept,
            last_window=key,
            windows_done=len(done),
        )
        if limit and windows_done >= limit:
            break
        t_hi = t_lo
    log.info("trades sweep: %d trades seen, %d kept on %d tickers", n_seen, n_kept, len(kept))


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
