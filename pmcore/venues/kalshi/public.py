"""Kalshi public REST client (no key). Read-only.

VERIFIED 2026-09-23 against docs/refs/kalshi-openapi.yaml and live calls (docs/observed/):
- Live base https://api.elections.kalshi.com/trade-api/v2; external-api.kalshi.com serves the
  same /historical/* responses (K-API-2).
- GET /historical/cutoff: trades_created_ts and market_settled_ts were 2026-07-25, so the whole
  in-sample window is historical.
- GET /historical/markets: limit <= 1000, cursor, and exactly ONE of tickers, event_ticker,
  series_ticker, mve_filter (observed 400 "mutually exclusive" when combining series_ticker with
  mve_filter). No time filters; pages newest close_time first.
- GET /historical/trades: ticker, min_ts, max_ts, limit <= 1000, cursor, is_block_trade.
- GET /historical/markets/{ticker}/candlesticks: start_ts, end_ts (inclusive), period_interval
  in {1, 60, 1440}; at most 5,000 candlesticks per request (observed error).
- GET /series: returns every series (14,327 observed) with category, fee_type, fee_multiplier.
- GET /series/fee_changes?show_historical=true and GET /events/fee_changes (cursor).
- Public rate limit is unpublished; a burst of sequential calls produced 429
  {"error":{"code":"too_many_requests"}}. Default 2 requests/second with backoff.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pmcore.venues.http import JsonClient
from pmcore.venues.kalshi.auth import signer_from_env

MAX_CANDLES_PER_REQUEST = 5000


class KalshiPublic:
    def __init__(
        self,
        base_url: str,
        historical_base_url: str | None = None,
        *,
        ca_bundle: str | None = None,
        rps: float = 2.0,
    ) -> None:
        signer = signer_from_env()
        sign = signer.sign if signer else None
        self.authenticated = signer is not None
        self.live = JsonClient(base_url, ca_bundle=ca_bundle, rps=rps, sign=sign)
        hist = historical_base_url or base_url
        self.hist = (
            self.live
            if hist == base_url
            else JsonClient(hist, ca_bundle=ca_bundle, rps=rps, sign=sign)
        )

    def close(self) -> None:
        self.live.close()
        if self.hist is not self.live:
            self.hist.close()

    @staticmethod
    def _paginate(
        client: JsonClient,
        path: str,
        params: dict[str, Any],
        key: str,
        page_limit: int,
        start_cursor: str | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield (items, next_cursor) so callers can checkpoint the cursor."""
        cursor: str | None = start_cursor
        while True:
            data = client.get(path, {**params, "limit": page_limit, "cursor": cursor})
            items = data.get(key, []) or []
            cursor = data.get("cursor") or None
            yield items, cursor
            if not cursor or not items:
                return

    def exchange_status(self) -> Any:
        return self.live.get("/exchange/status")

    def series(self, series_ticker: str) -> Any:
        return self.live.get(f"/series/{series_ticker}")

    def list_series(self, category: str | None = None) -> list[dict[str, Any]]:
        data = self.live.get("/series", {"category": category})
        return list(data.get("series", []) or [])

    def series_fee_changes(self, show_historical: bool = True) -> Any:
        return self.live.get(
            "/series/fee_changes", {"show_historical": str(show_historical).lower()}
        )

    def events_fee_changes(
        self, page_limit: int = 1000
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(self.live, "/events/fee_changes", {}, "event_fee_changes", page_limit)

    def event(self, event_ticker: str, with_nested_markets: bool = False) -> Any:
        return self.live.get(
            f"/events/{event_ticker}", {"with_nested_markets": str(with_nested_markets).lower()}
        )

    def events(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        min_close_ts: int | None = None,
        with_nested_markets: bool = False,
        page_limit: int = 200,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(
            self.live,
            "/events",
            {
                "status": status,
                "series_ticker": series_ticker,
                "min_close_ts": min_close_ts,
                "with_nested_markets": str(with_nested_markets).lower(),
            },
            "events",
            page_limit,
        )

    def markets(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        mve_filter: str | None = None,
        page_limit: int = 1000,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(
            self.live,
            "/markets",
            {
                "status": status,
                "series_ticker": series_ticker,
                "event_ticker": event_ticker,
                "min_close_ts": min_close_ts,
                "max_close_ts": max_close_ts,
                "mve_filter": mve_filter,
            },
            "markets",
            page_limit,
        )

    def market(self, ticker: str) -> Any:
        return self.live.get(f"/markets/{ticker}")

    def trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        page_limit: int = 1000,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(
            self.live,
            "/markets/trades",
            {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts},
            "trades",
            page_limit,
        )

    def candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> Any:
        return self.live.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )

    def historical_cutoff(self) -> Any:
        return self.hist.get("/historical/cutoff")

    def historical_market(self, ticker: str) -> Any:
        return self.hist.get(f"/historical/markets/{ticker}")

    def historical_markets(
        self,
        *,
        tickers: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        mve_filter: str | None = "exclude",
        page_limit: int = 1000,
        start_cursor: str | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(
            self.hist,
            "/historical/markets",
            {
                "tickers": tickers,
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "mve_filter": mve_filter,
            },
            "markets",
            page_limit,
            start_cursor,
        )

    def historical_trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        is_block_trade: bool | None = None,
        page_limit: int = 1000,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self._paginate(
            self.hist,
            "/historical/trades",
            {
                "ticker": ticker,
                "min_ts": min_ts,
                "max_ts": max_ts,
                "is_block_trade": None if is_block_trade is None else str(is_block_trade).lower(),
            },
            "trades",
            page_limit,
        )

    def historical_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> Any:
        return self.hist.get(
            f"/historical/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
